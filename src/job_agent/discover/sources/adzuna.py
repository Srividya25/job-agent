"""Adzuna — the aggregator lane, filtered server-side to fresh postings.

The company boards (Greenhouse/Lever/Ashby) cover the ~60 registered
employers completely. Adzuna covers the rest of the market the way a
"posted in the last 24 hours" board filter does: one search per wanted
title, `max_days_old=1`, against everything Adzuna indexes.

Dormant without ADZUNA_APP_ID / ADZUNA_APP_KEY in .env (free at
developer.adzuna.com). Every posting still walks the same funnel —
dedupe, staffing filter, sponsorship, title gate, score — so this widens
the top of the funnel without loosening anything below it.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from ...models import RawPosting

API = "https://api.adzuna.com/v1/api/jobs/us/search/1"
RESULTS_PER_TITLE = 50


def parse_results(payload: dict) -> list[RawPosting]:
    """Adzuna's JSON into RawPostings. Tolerant of missing fields."""
    postings: list[RawPosting] = []
    for row in payload.get("results", []) or []:
        url = row.get("redirect_url") or ""
        company = ((row.get("company") or {}).get("display_name") or "").strip()
        title = (row.get("title") or "").replace("<strong>", "").replace(
            "</strong>", "").strip()
        if not url or not company or not title:
            continue
        posted = None
        try:
            posted = datetime.fromisoformat(
                (row.get("created") or "").replace("Z", "+00:00")
            )
        except ValueError:
            pass
        postings.append(RawPosting(
            source="adzuna",
            company_name=company,
            title=title,
            url=url,
            location=((row.get("location") or {}).get("display_name")),
            description=row.get("description"),
            posted_at=posted,
            external_id=str(row.get("id") or "") or None,
        ))
    return postings


async def fetch_fresh(
    client: httpx.AsyncClient,
    app_id: str,
    app_key: str,
    titles: list[str],
    max_days_old: int = 1,
) -> list[RawPosting]:
    """One search per wanted title, server-filtered to fresh postings."""
    postings: list[RawPosting] = []
    for title in titles[:6]:
        try:
            r = await client.get(API, params={
                "app_id": app_id,
                "app_key": app_key,
                "what": title,
                "max_days_old": max_days_old,
                "results_per_page": RESULTS_PER_TITLE,
                "sort_by": "date",
            })
            if r.status_code != 200:
                continue
            postings.extend(parse_results(r.json()))
        except (httpx.HTTPError, ValueError):
            continue
    return postings
