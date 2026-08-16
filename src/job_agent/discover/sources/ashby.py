"""Ashby job boards.

    GET https://api.ashbyhq.com/posting-api/job-board/{slug}

Ashby ships `descriptionPlain`, so no HTML work. Two quirks handled here:
titles frequently carry leading whitespace, and unlisted drafts appear in the
payload with `isListed: false`.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from ...models import ATS, RawPosting
from ..base import get_json

name = "ashby"
BASE = "https://api.ashbyhq.com/posting-api/job-board"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _location(job: dict) -> str | None:
    primary = (job.get("location") or "").strip()
    extras = [
        (loc.get("location") or "").strip()
        for loc in job.get("secondaryLocations") or []
    ]
    parts = [p for p in [primary, *extras] if p]
    return " | ".join(dict.fromkeys(parts)) or None


async def fetch(
    client: httpx.AsyncClient,
    slug: str,
    search: list[str] | None = None,  # server-side filtering: Workday only
) -> list[RawPosting]:
    data = await get_json(client, f"{BASE}/{slug}?includeCompensation=true")
    if not isinstance(data, dict):
        return []

    out: list[RawPosting] = []
    for job in data.get("jobs", []):
        if job.get("isListed") is False:
            continue  # unpublished draft

        description = job.get("descriptionPlain") or ""
        comp = (job.get("compensation") or {}).get("compensationTierSummary")
        if comp:
            description = f"{description}\n\nCompensation: {comp}"

        out.append(
            RawPosting(
                source=name,
                company_name=slug,
                title=(job.get("title") or "").strip(),  # leading space is common
                url=job.get("applyUrl") or job.get("jobUrl") or "",
                location=_location(job),
                description=description.strip(),
                posted_at=_parse_dt(job.get("publishedAt")),
                external_id=job.get("id"),
                ats=ATS.ASHBY,
                ats_slug=slug,
            )
        )
    return out


async def probe(client: httpx.AsyncClient, slug: str) -> bool:
    data = await get_json(client, f"{BASE}/{slug}")
    return isinstance(data, dict) and bool(data.get("jobs"))
