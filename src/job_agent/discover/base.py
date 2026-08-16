"""Shared plumbing for discovery sources.

A source does exactly one thing: given a company slug, return `RawPosting`s.
It never touches the database, the scorer, or the filters. That is what keeps
adding a source down to a single small file.
"""

from __future__ import annotations

import html
import re
from typing import Protocol

import httpx

from ..models import RawPosting

USER_AGENT = "job-agent/0.1 (+https://github.com/Srividya25/job-agent)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\xa0]+")
_NEWLINES = re.compile(r"\n{3,}")


def html_to_text(raw: str | None) -> str:
    """Flatten an HTML job description to readable plain text.

    Greenhouse double-escapes its `content` field (`&lt;div&gt;` rather than
    `<div>`), so unescape runs twice — the second pass is a no-op on sources
    that only escape once.
    """
    if not raw:
        return ""
    s = html.unescape(html.unescape(raw))
    s = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.I)
    s = _TAG.sub("", s)
    s = _WS.sub(" ", s)
    return _NEWLINES.sub("\n\n", s).strip()


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


class Source(Protocol):
    """Every ATS adapter implements this."""

    name: str

    async def fetch(self, client: httpx.AsyncClient, slug: str) -> list[RawPosting]:
        """All currently-open postings for one company board."""
        ...

    async def probe(self, client: httpx.AsyncClient, slug: str) -> bool:
        """Does this company use this ATS under this slug?"""
        ...


async def get_json(client: httpx.AsyncClient, url: str) -> dict | list | None:
    """GET returning parsed JSON, or None on any failure.

    Discovery runs across dozens of boards; one dead board must never abort
    the nightly run, so failures are swallowed and reported as "no postings".
    """
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None
