"""Greenhouse job boards.

    GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

Public, unauthenticated, one call per company. `content=true` returns the full
description inline, which saves a request per posting.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from ...models import ATS, RawPosting
from ..base import get_json, html_to_text

name = "greenhouse"
BASE = "https://boards-api.greenhouse.io/v1/boards"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def fetch(
    client: httpx.AsyncClient,
    slug: str,
    search: list[str] | None = None,  # server-side filtering: Workday only
) -> list[RawPosting]:
    data = await get_json(client, f"{BASE}/{slug}/jobs?content=true")
    if not isinstance(data, dict):
        return []

    out: list[RawPosting] = []
    for job in data.get("jobs", []):
        location = (job.get("location") or {}).get("name") or None
        # company_name is per-posting in Greenhouse and occasionally absent;
        # the slug is the reliable fallback.
        company = job.get("company_name") or slug

        out.append(
            RawPosting(
                source=name,
                company_name=company.strip(),
                title=(job.get("title") or "").strip(),
                url=job.get("absolute_url") or "",
                location=location.strip() if location else None,
                description=html_to_text(job.get("content")),
                posted_at=_parse_dt(job.get("first_published"))
                or _parse_dt(job.get("updated_at")),
                external_id=str(job.get("id")) if job.get("id") else None,
                ats=ATS.GREENHOUSE,
                ats_slug=slug,
            )
        )
    return out


async def probe(client: httpx.AsyncClient, slug: str) -> bool:
    data = await get_json(client, f"{BASE}/{slug}/jobs")
    return isinstance(data, dict) and bool(data.get("jobs"))
