"""Target company list and ATS detection.

The seed list below is a starting point only. The authoritative list is
derived from the USCIS data: companies with real filing history, minus
staffing firms, that have a pollable board (`job-agent companies expand`).

Slugs are guesses. `discover_ats` probes each board and records what actually
answers, so a wrong guess costs one HTTP call and nothing else.
"""

from __future__ import annotations

import asyncio
import re

import httpx

from ..models import ATS
from .sources import PROBE_ORDER, REGISTRY

# --------------------------------------------------------------------------
# Seed list. Grouped only for readability — the code treats them the same.
# --------------------------------------------------------------------------

BIG_TECH = [
    "google", "meta", "amazon", "microsoft", "apple", "nvidia", "netflix",
    "adobe", "salesforce", "oracle", "intel", "qualcomm", "cisco", "uber",
    "lyft", "airbnb", "doordash", "pinterest", "snap", "paypal", "ebay",
    "intuit", "workday", "servicenow", "splunk", "autodesk", "zoom",
    "atlassian", "dropbox", "linkedin", "tesla",
    # Workday tenants (see sources/workday.py BOARDS)
    "hp", "micron",
]

DATA_INFRA = [
    "databricks", "snowflake", "confluent", "mongodb", "elastic", "datadog",
    "cloudflare", "hashicorp", "okta", "twilio", "stripe", "sentry",
    "amplitude", "fastly", "gitlab", "docker", "grafana", "temporal",
]

SCALEUPS = [
    "openai", "anthropic", "scaleai", "figma", "notion", "rippling", "ramp",
    "brex", "plaid", "airtable", "retool", "deel", "gusto", "samsara",
    "discord", "reddit", "asana", "grammarly", "duolingo", "affirm",
    "carta", "benchling", "instacart", "robinhood", "coinbase", "chime",
    "vercel", "linear", "sourcegraph", "mercury", "modernhealth",
]

FINANCE = [
    "bloomberg", "capitalone", "visa", "mastercard", "americanexpress",
    "twosigma", "citadel", "janestreet", "point72", "hudsonrivertrading",
]

MOBILITY = ["rivian", "lucidmotors", "waymo", "zoox", "aurora", "nuro"]

# Cap-exempt employers are exempt from the H-1B lottery entirely — no March
# gamble, file any time of year. Most candidates never look here.
CAP_EXEMPT = [
    "stanford", "berkeley", "ucsf", "mit", "harvard", "caltech",
    "cmu", "gatech", "umich", "uw", "nyu", "columbia",
    "broadinstitute", "chanzuckerberg", "alleninstitute",
]

SEED_COMPANIES: list[str] = [
    *BIG_TECH, *DATA_INFRA, *SCALEUPS, *FINANCE, *MOBILITY, *CAP_EXEMPT,
]

# Defense and export-controlled employers cannot hire candidates who need
# sponsorship, regardless of filing history. Never seed them.
NEVER_SEED = {
    "spacex", "anduril", "lockheedmartin", "raytheon", "northropgrumman",
    "generaldynamics", "l3harris", "boeingdefense", "palantirfederal",
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


async def discover_ats(
    client: httpx.AsyncClient, slug: str
) -> tuple[ATS, str] | None:
    """Probe each ATS for this slug. Returns (ats, slug) or None."""
    for ats in PROBE_ORDER:
        try:
            if await REGISTRY[ats].probe(client, slug):
                return ats, slug
        except httpx.HTTPError:
            continue
    return None


async def discover_many(
    client: httpx.AsyncClient,
    slugs: list[str],
    concurrency: int = 8,
) -> dict[str, tuple[ATS, str]]:
    """Resolve many companies at once, politely."""
    semaphore = asyncio.Semaphore(concurrency)
    found: dict[str, tuple[ATS, str]] = {}

    async def one(slug: str) -> None:
        async with semaphore:
            if result := await discover_ats(client, slug):
                found[slug] = result

    await asyncio.gather(*(one(s) for s in slugs if s not in NEVER_SEED))
    return found
