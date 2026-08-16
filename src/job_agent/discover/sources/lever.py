"""Lever job boards.

    GET https://api.lever.co/v0/postings/{slug}?mode=json

Returns a bare JSON array. Lever already ships plain-text variants of every
rich field, so no HTML stripping is needed here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from ...models import ATS, RawPosting
from ..base import get_json

name = "lever"
BASE = "https://api.lever.co/v0/postings"


def _parse_epoch_ms(value: int | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, OSError, TypeError):
        return None


def _description(job: dict) -> str:
    """Lever splits the description across several fields; stitch them back.

    `lists` holds the bulleted sections ("What You'll Do", requirements) which
    is where most skill keywords live — dropping it would gut the match score.
    """
    parts = [job.get("descriptionPlain") or ""]
    for block in job.get("lists") or []:
        heading = (block.get("text") or "").strip()
        body = block.get("content") or ""
        # `content` is an HTML fragment of <li> items.
        body = re.sub(r"<li[^>]*>", "\n• ", body)
        body = re.sub(r"<[^>]+>", "", body)
        parts.append(f"\n{heading}\n{body}".rstrip())
    parts.append(job.get("additionalPlain") or "")
    return "\n\n".join(p for p in parts if p.strip()).strip()


async def fetch(
    client: httpx.AsyncClient,
    slug: str,
    search: list[str] | None = None,  # server-side filtering: Workday only
) -> list[RawPosting]:
    data = await get_json(client, f"{BASE}/{slug}?mode=json")
    if not isinstance(data, list):
        return []

    out: list[RawPosting] = []
    for job in data:
        cats = job.get("categories") or {}
        out.append(
            RawPosting(
                source=name,
                company_name=slug,  # Lever does not return a display name
                title=(job.get("text") or "").strip(),
                url=job.get("applyUrl") or job.get("hostedUrl") or "",
                location=(cats.get("location") or "").strip() or None,
                description=_description(job),
                posted_at=_parse_epoch_ms(job.get("createdAt")),
                external_id=job.get("id"),
                ats=ATS.LEVER,
                ats_slug=slug,
            )
        )
    return out


async def probe(client: httpx.AsyncClient, slug: str) -> bool:
    data = await get_json(client, f"{BASE}/{slug}?mode=json")
    return isinstance(data, list) and len(data) > 0
