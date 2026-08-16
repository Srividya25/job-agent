"""Core data types.

The pipeline is a series of narrowing transforms:

    RawPosting  ──canonicalize──>  Job  ──filter──>  Job  ──score──>  Job
    (per source)                   (deduped)         (+verdict)       (+score)

Every discovery source emits `RawPosting` and nothing else. That is the whole
reason a new source is one small file: it never learns about scoring, filtering,
or the database.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# normalization helpers
#
# Company and title strings arrive spelled a dozen ways across sources
# ("Stripe", "Stripe, Inc.", "STRIPE INC"). Every comparison in the codebase
# goes through these, so the rules live in exactly one place.
# --------------------------------------------------------------------------

_LEGAL_SUFFIXES = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c\.|ltd|limited|corp|corporation|co|company|"
    r"plc|gmbh|sa|nv|bv|ag|pvt|private|holdings|group|technologies|"
    r"labs|labs\.|the)\b",
    re.I,
)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalize_company(name: str) -> str:
    """'Stripe, Inc.' and 'STRIPE' collapse to the same key.

    Deliberately aggressive: a false merge of two companies is far less
    costly here than failing to recognize that we already know one.
    """
    s = _PUNCT.sub(" ", name.lower())
    s = _LEGAL_SUFFIXES.sub(" ", s)
    return _WS.sub(" ", s).strip()


def normalize_title(title: str) -> str:
    """Strip req IDs, locations and seniority noise from a job title."""
    s = title.lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)          # "(Remote)", "[R12345]"
    s = re.sub(r"\b(req|requisition|job)\s*#?\s*\d+", " ", s)
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


_DURATION = re.compile(r"^\s*(\d+)\s*([hdwm])\w*\s*$", re.I)
_UNIT_HOURS = {"h": 1, "d": 24, "w": 168, "m": 720}

# Named windows, so common cases need no arithmetic from the user.
DURATION_ALIASES = {
    "today": 24, "24h": 24, "day": 24,
    "3d": 72, "3days": 72,
    "week": 168, "1w": 168, "7d": 168,
    "2w": 336, "fortnight": 336,
    "month": 720, "30d": 720,
    "all": 0, "any": 0,
}


def parse_since(value: str | None) -> int:
    """Turn '24h' / '3d' / '1w' / 'today' into hours. 0 means no limit.

    Raises ValueError on anything unrecognized rather than silently falling
    back to "everything" — a typo that quietly widens the search to a year of
    postings is worse than an error.
    """
    if not value:
        return 0
    key = value.strip().lower()
    if key in DURATION_ALIASES:
        return DURATION_ALIASES[key]
    if match := _DURATION.match(key):
        amount, unit = int(match.group(1)), match.group(2).lower()
        return amount * _UNIT_HOURS[unit]
    raise ValueError(
        f"unrecognized time window: {value!r}. "
        "Try 24h, 3d, 1w, 30d, or 'all'."
    )


def normalize_location(loc: str | None) -> str:
    if not loc:
        return ""
    s = _PUNCT.sub(" ", loc.lower())
    s = re.sub(r"\b(united states|usa|us|remote|hybrid|onsite)\b", " ", s)
    return _WS.sub(" ", s).strip()


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------


class ATS(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"
    WORKDAY = "workday"
    UNKNOWN = "unknown"


# Aggregator sources (Adzuna, alerts) hand over Workday URLs with ats left
# UNKNOWN, so the enum alone under-counts Workday. Anything that must treat
# Workday specially checks the URL too.
_WORKDAY_URL = re.compile(r"//[^/]*(?:myworkdayjobs|myworkdaysite)\.com", re.I)


def is_workday(ats: "ATS", url: str | None) -> bool:
    return ats is ATS.WORKDAY or bool(url and _WORKDAY_URL.search(url))


class Verdict(StrEnum):
    """Outcome of the company-eligibility check.

    ASK exists on purpose. A wrong BLOCK is invisible and costs a real
    opportunity; a wrong ALLOW costs ten seconds of review. When the
    signals disagree, we ask rather than guess.
    """

    ALLOW = "allow"
    BLOCK = "block"
    ASK = "ask"


class JobStatus(StrEnum):
    NEW = "new"
    APPROVED = "approved"
    SKIPPED = "skipped"
    APPLIED = "applied"
    NEEDS_REVIEW = "needs_review"
    PENDING_APPROVAL = "pending_approval"
    BLOCKED = "blocked"
    # Aged-out postings. Written by an early cleanup pass and still present
    # in real databases; without this member, deserializing any such row
    # crashes whatever touched it (job-agent yours found that the hard way).
    STALE = "stale"


# --------------------------------------------------------------------------
# pipeline types
# --------------------------------------------------------------------------


class RawPosting(BaseModel):
    """The only thing a discovery source is allowed to produce."""

    source: str
    company_name: str
    title: str
    url: str
    location: str | None = None
    description: str | None = None
    posted_at: datetime | None = None
    external_id: str | None = None
    ats: ATS | None = None
    ats_slug: str | None = None

    @property
    def dedupe_key(self) -> str:
        """Identity of a job across sources.

        The same role reaches us from Greenhouse, Adzuna and a LinkedIn
        alert with three different URLs, so the URL cannot be the key.
        Company + title + location can.
        """
        parts = "|".join(
            (
                normalize_company(self.company_name),
                normalize_title(self.title),
                normalize_location(self.location),
            )
        )
        return hashlib.sha256(parts.encode()).hexdigest()[:16]


class StaffingSignal(BaseModel):
    matched: bool = False
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)


class SponsorshipSignal(BaseModel):
    """Whether this employer sponsors work visas.

    `filings` is authoritative (they demonstrably have sponsored);
    `jd_blocks` overrides it (this specific req says no).
    """

    filings: dict[str, int] = Field(default_factory=dict)
    total_filings: int = 0
    # Legal entities the name matched. Surfaced so an over-broad subsidiary
    # match is auditable rather than silent.
    matched_employers: list[str] = Field(default_factory=list)
    jd_blocks: list[str] = Field(default_factory=list)
    jd_confirms: list[str] = Field(default_factory=list)

    @property
    def score(self) -> float:
        if self.jd_blocks:
            return 0.0
        if self.jd_confirms:
            return 1.0
        if self.total_filings >= 50:
            return 0.9
        if self.total_filings >= 5:
            return 0.7
        if self.total_filings > 0:
            return 0.5
        return 0.2  # unknown, not disproven


class Company(BaseModel):
    name: str
    normalized_name: str
    ats: ATS = ATS.UNKNOWN
    ats_slug: str | None = None

    staffing: StaffingSignal = Field(default_factory=StaffingSignal)
    sponsorship: SponsorshipSignal = Field(default_factory=SponsorshipSignal)
    legit_score: float = 0.5

    verdict: Verdict = Verdict.ASK
    verdict_reason: str = ""
    checked_at: datetime | None = None


class MatchBreakdown(BaseModel):
    """Every score is shown with its parts, never as a bare number.

    A single '87%' is not reviewable. The components are, and they are what
    let us calibrate the threshold against real judgement.
    """

    skills: float = 0.0
    title: float = 0.0
    semantic: float = 0.0
    recency: float = 0.0
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)

    @property
    def total(self) -> float:
        return round(
            0.40 * self.skills
            + 0.25 * self.title
            + 0.25 * self.semantic
            + 0.10 * self.recency,
            4,
        )


class Job(BaseModel):
    dedupe_key: str
    company: str
    title: str
    url: str
    location: str | None = None
    description: str | None = None
    posted_at: datetime | None = None

    sources: list[str] = Field(default_factory=list)
    ats: ATS = ATS.UNKNOWN

    verdict: Verdict = Verdict.ASK
    verdict_reason: str = ""

    match_score: float = 0.0
    match_breakdown: MatchBreakdown | None = None
    best_resume: str | None = None

    status: JobStatus = JobStatus.NEW
    discovered_at: date = Field(default_factory=date.today)

    @classmethod
    def from_posting(cls, p: RawPosting) -> Job:
        return cls(
            dedupe_key=p.dedupe_key,
            company=p.company_name,
            title=p.title,
            url=p.url,
            location=p.location,
            description=p.description,
            posted_at=p.posted_at,
            sources=[p.source],
            ats=p.ats or ATS.UNKNOWN,
        )
