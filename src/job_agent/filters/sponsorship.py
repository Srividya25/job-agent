"""Visa sponsorship signals.

Two independent inputs:

  1. USCIS H-1B Employer Data Hub — authoritative record of who HAS sponsored.
  2. The job description — authoritative record of whether THIS req will.

(2) always overrides (1). A company with 5,000 approvals still posts roles
that say "no sponsorship", and clearance/ITAR roles can never sponsor
regardless of history.
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

from ..config import data_dir
from ..models import SponsorshipSignal, normalize_company

# --------------------------------------------------------------------------
# Job-description scanning
# --------------------------------------------------------------------------

# Any hit here disqualifies the posting outright.
_HARD_BLOCK = [
    (r"\bunable to (offer |provide )?(visa )?sponsor", "states it cannot sponsor"),
    # Up to two words between the negation and "sponsor": "does not offer
    # visa sponsorship", "will not currently provide sponsorship", …
    (r"\b(do(es)? not|won't|will not|cannot|can't)\s+(?:\w+\s+){0,2}sponsor",
     "states it will not sponsor"),
    (r"\bno (visa )?sponsorship\b", "says no sponsorship"),
    (r"without (the need for )?(visa )?sponsorship", "requires no-sponsorship status"),
    (r"\bnot (be )?(able|eligible) to sponsor", "cannot sponsor"),
    (r"must not require sponsorship", "excludes sponsorship needs"),
    (r"\b(us|u\.s\.) citizens?( or | / )?(green card|permanent resident)?s? only",
     "US citizens/GC only"),
    # Clearance and export-control roles are legally closed to visa holders.
    (r"\bsecurity clearance\b", "requires security clearance"),
    (r"\b(ts/sci|top secret|secret clearance)\b", "requires clearance"),
    (r"\bitar\b|\bexport control", "ITAR / export controlled"),
    (r"\bmust be a (us|u\.s\.) (citizen|person)\b", "US citizen required"),
    (r"\b(us|u\.s\.) citizenship (is )?required", "US citizenship required"),
]

_CONFIRMS = [
    (r"\b(visa )?sponsorship (is )?available", "explicitly offers sponsorship"),
    (r"\bwe (do )?sponsor\b", "states it sponsors"),
    (r"\b(h-?1b|h1-b) sponsorship", "mentions H-1B sponsorship"),
    (r"\bwilling to sponsor", "willing to sponsor"),
    (r"\bsponsor(ship)? for (work )?(visas?|authorization)", "offers visa sponsorship"),
]


# "no security clearance required" / "does not require a clearance" is the
# opposite of a requirement. Any clearance hit whose only occurrences sit in
# a negating phrase like these is not a block.
_CLEARANCE_NEGATED = re.compile(
    r"\b(?:no|not|without|does\s?n[o']t\s+require(?:\s+an?|\s+active)?)"
    r"[^.\n]{0,50}?(?:security\s+)?clearance",
    re.I,
)
_CLEARANCE_PATTERNS = re.compile(r"clearance", re.I)


def scan_description(description: str | None) -> tuple[list[str], list[str]]:
    """Return (blocking reasons, confirming reasons) found in the JD."""
    if not description:
        return [], []
    text = description.lower()
    negated_clearance = bool(_CLEARANCE_NEGATED.search(text)) and (
        len(_CLEARANCE_NEGATED.findall(text))
        >= len(_CLEARANCE_PATTERNS.findall(text))
    )
    blocks = [
        why for pat, why in _HARD_BLOCK
        if re.search(pat, text, re.I)
        and not (negated_clearance and "clearance" in why)
    ]
    confirms = [why for pat, why in _CONFIRMS if re.search(pat, text, re.I)]
    return blocks, confirms


# --------------------------------------------------------------------------
# USCIS H-1B Employer Data Hub
#
# Download the yearly CSVs to data/h1b/ (see `job-agent data h1b --help`).
# Absent the files, sponsorship falls back to JD text alone and every
# unknown company scores "unproven" rather than "rejected".
# --------------------------------------------------------------------------

H1B_DIR_NAME = "h1b"

_EMPLOYER_COLUMNS = ("Employer (Petitioner) Name", "Employer", "EMPLOYER_NAME")
_APPROVAL_COLUMNS = (
    "Initial Approval",
    "Initial Approvals",
    "Continuing Approval",
    "Continuing Approvals",
)


def h1b_dir() -> Path:
    d = data_dir() / H1B_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _year_from_filename(path: Path) -> str:
    m = re.search(r"(20\d{2})", path.name)
    return m.group(1) if m else path.stem


@lru_cache(maxsize=1)
def load_h1b_index() -> dict[str, dict[str, int]]:
    """normalized employer name -> {year: approvals}.

    Tolerant of column-name drift across USCIS export vintages: it looks for
    any recognized employer/approval header rather than a fixed schema.
    """
    index: dict[str, dict[str, int]] = {}
    directory = h1b_dir()

    for csv_path in sorted(directory.glob("*.csv")):
        year = _year_from_filename(csv_path)
        try:
            with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
                reader = csv.DictReader(fh)
                if not reader.fieldnames:
                    continue
                name_col = next(
                    (c for c in _EMPLOYER_COLUMNS if c in reader.fieldnames), None
                )
                if not name_col:
                    continue
                approval_cols = [
                    c for c in _APPROVAL_COLUMNS if c in reader.fieldnames
                ]

                for row in reader:
                    raw_name = (row.get(name_col) or "").strip()
                    if not raw_name:
                        continue
                    key = normalize_company(raw_name)
                    total = 0
                    for col in approval_cols:
                        value = (row.get(col) or "0").replace(",", "").strip()
                        total += int(value) if value.isdigit() else 0
                    bucket = index.setdefault(key, {})
                    bucket[year] = bucket.get(year, 0) + total
        except (OSError, csv.Error):
            continue

    return index


# Brand name != legal entity. USCIS records the filer, so Meta appears as
# FACEBOOK INC and Alphabet as GOOGLE LLC. Prefix matching cannot bridge that,
# and for short brand names it actively harms: "block" sweeps up BLOCK CHAIN
# TECHNOLOGY SOLUTIONS, BLOCK SCIENCE, BLOCK TACKLE...
#
#   value = dataset names to use instead of the brand name. Values are run
#   through normalize_company() on use, so write them however reads clearly.
_ALIASES: dict[str, tuple[str, ...]] = {
    "meta": ("meta platforms", "facebook"),
    "alphabet": ("google",),
    "x": ("x corp", "twitter"),
    "block": ("block inc", "square inc"),
    "square": ("square inc", "block inc"),
    "cruise": ("gm cruise", "cruise llc"),
}

# Names where a prefix sweep does more harm than good: too short, or a common
# English word that prefixes many unrelated firms.
_EXACT_ONLY = {
    "x", "box", "lyft", "bolt", "atom", "sift", "opal", "verb",
    "block", "square", "ramp", "notion", "linear", "scale", "figma",
}


@lru_cache(maxsize=4096)
def lookup_filings_detailed(company_name: str) -> tuple[dict[str, int], tuple[str, ...]]:
    """Filings for a company, summed across its subsidiaries.

    Employers file under their legal entity, so a single company is scattered
    across many rows: Amazon appears as AMAZON COM SERVICES LLC, AMAZON DATA
    SERVICES INC, AMAZON ADVERTISING LLC and so on. Exact match finds none of
    them, which is why a whole-word prefix pass runs when exact match fails.

    Returns (filings by year, names that matched) — the names are surfaced in
    the UI so an over-broad match is visible rather than silent.
    """
    index = load_h1b_index()
    key = normalize_company(company_name)
    if not key:
        return {}, ()

    # Curated alias wins over both exact and prefix matching.
    search_keys = tuple(
        normalize_company(a) for a in _ALIASES.get(key, (key,))
    )

    totals: dict[str, int] = {}
    matched: list[str] = []

    for search in search_keys:
        if exact := index.get(search):
            matched.append(search)
            for year, count in exact.items():
                totals[year] = totals.get(year, 0) + count

        # Prefix sweep picks up subsidiaries (AMAZON DATA SERVICES INC etc).
        # A trailing space enforces a word boundary so "app" cannot hit "apple".
        if search in _EXACT_ONLY or len(search) < 4:
            continue
        prefix = search + " "
        for name, years in index.items():
            if name.startswith(prefix):
                matched.append(name)
                for year, count in years.items():
                    totals[year] = totals.get(year, 0) + count

    return totals, tuple(sorted(set(matched))[:8])


def lookup_filings(company_name: str) -> dict[str, int]:
    return lookup_filings_detailed(company_name)[0]


def assess(company_name: str, description: str | None = None) -> SponsorshipSignal:
    """Combine filing history with the job description."""
    blocks, confirms = scan_description(description)
    filings, matched = lookup_filings_detailed(company_name)
    return SponsorshipSignal(
        filings=filings,
        total_filings=sum(filings.values()),
        matched_employers=list(matched),
        jd_blocks=blocks,
        jd_confirms=confirms,
    )


def dataset_available() -> bool:
    return bool(load_h1b_index())


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

_USCIS_TEMPLATE = (
    "https://www.uscis.gov/sites/default/files/document/data/"
    "h1b_datahubexport-{year}.csv"
)

# Verified present as of this writing. USCIS publishes on its own schedule and
# has changed paths before, so a missing year is reported, never fabricated —
# drop newer CSVs into data/h1b/ by hand and they are picked up automatically.
KNOWN_YEARS = (2021, 2022, 2023)


def fetch_h1b_data(years: tuple[int, ...] = KNOWN_YEARS) -> list[tuple[int, str]]:
    """Download USCIS employer CSVs. Returns (year, status) per year."""
    import httpx

    results: list[tuple[int, str]] = []
    target = h1b_dir()

    with httpx.Client(timeout=180.0, follow_redirects=True) as client:
        for year in years:
            dest = target / f"h1b_datahubexport-{year}.csv"
            if dest.exists() and dest.stat().st_size > 0:
                results.append((year, "cached"))
                continue
            try:
                r = client.get(_USCIS_TEMPLATE.format(year=year))
                if r.status_code != 200 or "csv" not in r.headers.get(
                    "content-type", ""
                ):
                    results.append((year, f"unavailable (HTTP {r.status_code})"))
                    continue
                dest.write_bytes(r.content)
                results.append((year, f"downloaded {len(r.content) // 1024} KB"))
            except httpx.HTTPError as exc:
                results.append((year, f"failed: {type(exc).__name__}"))

    load_h1b_index.cache_clear()
    return results
