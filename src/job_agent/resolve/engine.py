"""Tiered field resolution.

    Tier 1  cache   remembered answer               free
    Tier 2  rules   deterministic label match       free
    Tier 3  llm     batched; Claude or local Qwen   costs money (or is local)

Tier is recorded on every answer, and the auto-submit gate (ADR-004) requires
every field to be tier <= 2. So the agent submits unattended only when it has
seen the whole form before — an LLM answer always goes to her review first,
whichever model produced it.
"""

from __future__ import annotations

from enum import IntEnum
from urllib.parse import urlparse

from pydantic import BaseModel

from ..config import Profile
from ..forms.extract import FormField
from . import cache, rules


class Tier(IntEnum):
    CACHE = 1
    RULES = 2
    LLM = 3
    UNRESOLVED = 99


class Answer(BaseModel):
    ref: str
    label: str
    value: str
    tier: Tier
    field_type: str
    required: bool = False

    @property
    def is_confident(self) -> bool:
        """Whether this answer may be submitted without review."""
        return self.tier <= Tier.RULES


class Resolution(BaseModel):
    answers: list[Answer]
    unresolved: list[FormField]

    @property
    def all_confident(self) -> bool:
        return all(a.is_confident for a in self.answers)

    @property
    def blocking(self) -> list[FormField]:
        """Required fields nothing could answer — these stop an auto-submit."""
        return [f for f in self.unresolved if f.required]

    def summary(self) -> dict[str, int]:
        counts = {"cache": 0, "rules": 0, "llm": 0, "unresolved": len(self.unresolved)}
        for answer in self.answers:
            counts[answer.tier.name.lower()] += 1
        return counts


def scope_for(url: str) -> str:
    """Cache scope: the portal host, so per-company overrides are possible."""
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.") or "*"


def resolve_fields(
    fields: list[FormField],
    profile: Profile,
    url: str = "",
    use_cache: bool = True,
    resume_path: str | None = None,
    # Off by default so tests, previews, and every other caller stay
    # network-free; the real fill path in apply.py opts in explicitly.
    use_llm: bool = False,
    # Job context for Tier 3 — the description is what grounds an essay
    # draft in the actual posting rather than in the model's imagination.
    company: str = "",
    title: str = "",
    description: str = "",
) -> Resolution:
    scope = scope_for(url) if url else "*"
    answers: list[Answer] = []
    unresolved: list[FormField] = []

    for field in fields:
        # File inputs are handled by rules even when unlabelled — the resume
        # is the one thing every application needs.
        if (not field.label and field.type != "file") or field.type == "hidden":
            unresolved.append(field)
            continue

        # Tier 1 — remembered
        if use_cache and (hit := cache.lookup(field.label, scope)):
            answers.append(
                Answer(ref=field.ref, label=field.label, value=hit.answer,
                       tier=Tier.CACHE, field_type=field.type,
                       required=field.required)
            )
            continue

        # Tier 2 — rules
        value = rules.resolve(field, profile, resume_path=resume_path)

        # A choice field may only be answered with one of its choices. Twilio
        # has a checkbox labelled "Careers Website", which the portfolio rule
        # happily answered with a URL — nonsense as a checkbox value, and the
        # URL then broke the selector built to find the option.
        if value and field.is_choice and field.options:
            from ..fill.writer import best_option

            if best_option(value, field.options) is None:
                unresolved.append(field)
                continue

        if value is not None:
            if value == "":
                # The rule matched and decided this stays blank. That is an
                # answer, not a gap — but there is nothing to type.
                continue
            answers.append(
                Answer(ref=field.ref, label=field.label, value=value,
                       tier=Tier.RULES, field_type=field.type,
                       required=field.required)
            )
            continue

        # Tier 3 candidates — batched into one call below.
        unresolved.append(field)

    # Tier 3 — one batched call for everything still open. Answers come back
    # keyed by ref; anything the model returned null for (or answered with a
    # value its own options list rejects) stays unresolved and gets asked.
    if use_llm and unresolved:
        from . import llm

        if llm.available():
            askable = [f for f in unresolved if f.label and f.type != "hidden"]
            got, _provider = llm.answer_fields(
                profile, askable, company=company, title=title,
                description=description,
            )
            if got:
                from ..fill.writer import best_option

                still: list[FormField] = []
                for field in unresolved:
                    value = got.get(field.ref, "")
                    if not value:
                        still.append(field)
                        continue
                    if field.is_choice and field.options and not all(
                        best_option(part, field.options)
                        for part in value.split(" | ")
                    ):
                        still.append(field)
                        continue
                    answers.append(
                        Answer(ref=field.ref, label=field.label, value=value,
                               tier=Tier.LLM, field_type=field.type,
                               required=field.required)
                    )
                unresolved = still

    return Resolution(answers=answers, unresolved=unresolved)


def learn(resolution: Resolution, url: str = "") -> int:
    """Persist answers so they become Tier 1 next time.

    Rule-derived answers are stored GLOBALLY, not against the portal host.
    They come from the profile — a name is a name on every site — so scoping
    them to a host meant a Workday form re-derived everything the Greenhouse
    forms had already learned, and any answer the user typed by hand would
    have to be typed again on each new portal.

    Answers that are not rule-derived stay host-scoped, because those are the
    genuinely company-specific ones ("Have you worked here before?").
    """
    saved = 0
    for answer in resolution.answers:
        # Never cache a file path. The resume varies per job (best-of-N) and
        # the path changes whenever resumes are reorganized, so a cached one
        # goes stale silently and uploads the wrong document. Caching one
        # meant an application was about to attach a superseded resume.
        if answer.field_type == "file":
            continue
        if answer.tier is Tier.RULES:
            cache.remember(answer.label, answer.value, "*", answer.field_type)
            saved += 1
    return saved


def remember_manual(
    label: str, value: str, field_type: str = "text", url: str = "", generic: bool = False
) -> None:
    """Record an answer the user supplied.

    `generic=True` marks it as portable across portals; the default keeps it
    scoped to this host, since most hand-answered questions are about one
    specific employer.
    """
    cache.remember(label, value, "*" if generic else scope_for(url), field_type)
