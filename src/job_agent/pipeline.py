"""The discovery pipeline.

    fetch -> dedupe -> company verdict -> gates -> score -> store

Filters run before scoring because they are nearly free and remove most of
the volume; there is no point scoring a body-shop posting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Profile
from .discover.base import make_client
from .discover.sources import REGISTRY
from .filters import company as company_filter
from .match.resume import ParsedResume
from .match.score import passes_gates, score_job
from .models import ATS, Job, RawPosting, Verdict, normalize_company
from .store import db


@dataclass
class RunStats:
    fetched: int = 0
    deduped: int = 0
    blocked: int = 0
    gated: int = 0
    scored: int = 0
    new_jobs: int = 0
    block_reasons: dict[str, int] = field(default_factory=dict)

    def note_block(self, reason: str) -> None:
        key = reason.split(";")[0][:60] or "unspecified"
        self.block_reasons[key] = self.block_reasons.get(key, 0) + 1


async def fetch_all(
    boards: dict[str, tuple[ATS, str]],
    concurrency: int = 6,
    on_board: Callable[[str, int], None] | None = None,
    search: list[str] | None = None,
) -> list[RawPosting]:
    """Pull every board concurrently. One dead board never aborts the run.

    `search` is passed through to sources that can filter server-side. Only
    Workday uses it, and for Workday it is close to essential: its tenants
    hold thousands of postings and each one costs a second request to
    hydrate, so unfiltered polling would be enormously wasteful.
    """
    semaphore = asyncio.Semaphore(concurrency)
    postings: list[RawPosting] = []

    async with make_client() as client:

        async def one(name: str, ats: ATS, slug: str) -> None:
            async with semaphore:
                try:
                    found = await REGISTRY[ats].fetch(client, slug, search=search)
                except Exception:  # noqa: BLE001 - a bad board must not kill the run
                    found = []
                postings.extend(found)
                if on_board:
                    on_board(name, len(found))

        await asyncio.gather(
            *(one(name, ats, slug) for name, (ats, slug) in boards.items())
        )

    return postings


def dedupe(postings: list[RawPosting]) -> list[Job]:
    """Collapse the same role arriving from several sources into one Job."""
    jobs: dict[str, Job] = {}
    for posting in postings:
        key = posting.dedupe_key
        if existing := jobs.get(key):
            if posting.source not in existing.sources:
                existing.sources.append(posting.source)
            # Prefer whichever source gave us the richer description.
            if len(posting.description or "") > len(existing.description or ""):
                existing.description = posting.description
            continue
        jobs[key] = Job.from_posting(posting)
    return list(jobs.values())


def process(
    jobs: list[Job],
    profile: Profile,
    resumes: list[ParsedResume],
    overrides: dict[str, Verdict] | None = None,
) -> tuple[list[Job], RunStats]:
    """Apply company verdicts, hard gates and scoring."""
    stats = RunStats(fetched=len(jobs), deduped=len(jobs))
    company_cache: dict[str, tuple[Verdict, str]] = {}
    kept: list[Job] = []

    for job in jobs:
        # Company verdict is cached per company, but the sponsorship scan is
        # per-posting: the same employer can post one role that sponsors and
        # another that does not.
        normalized = normalize_company(job.company)
        verdict_info = company_filter.evaluate(
            job.company,
            profile,
            description=job.description,
            ats=job.ats,
            overrides=overrides,
        )
        job.verdict = verdict_info.verdict
        job.verdict_reason = verdict_info.verdict_reason
        company_cache.setdefault(normalized, (job.verdict, job.verdict_reason))

        if job.verdict is Verdict.BLOCK:
            stats.blocked += 1
            stats.note_block(job.verdict_reason)
            continue

        ok, why = passes_gates(job, profile)
        if not ok:
            stats.gated += 1
            stats.note_block(why)
            continue

        job.match_score, job.match_breakdown, job.best_resume = score_job(
            job, profile, resumes
        )
        stats.scored += 1
        kept.append(job)

    kept.sort(key=lambda j: j.match_score, reverse=True)
    return kept, stats


def persist(jobs: list[Job], stats: RunStats) -> RunStats:
    with db.connect() as conn:
        for job in jobs:
            if db.upsert_job(conn, job):
                stats.new_jobs += 1
    return stats
