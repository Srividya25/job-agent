"""Workday tenants.

Workday is the odd one out and needs its own shape:

  list    POST {tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
          {"appliedFacets":{}, "limit":20, "offset":0, "searchText":"..."}
  detail  GET  .../wday/cxs/{tenant}/{site}{externalPath}

Three consequences:

1. The list response carries no description and only a human-readable
   "Posted 7 Days Ago", so every posting needs a second call to be useful.
2. Tenants are sharded across wd1/wd3/wd5/... and the site path is an
   arbitrary per-company string ("Cisco_Careers", "jobs", "External").
   A probe of 3,885 shard x site combinations resolved 4 of 37 companies,
   so these are registered, never guessed — see `from_url`.
3. Tenants are large (NVIDIA alone posts 2,000 roles), which makes the N+1
   hydration expensive. `search` pushes filtering server-side instead.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import httpx

from ...models import ATS, RawPosting
from ..base import get_json, html_to_text

name = "workday"

PAGE_SIZE = 20
MAX_PAGES = 15          # 300 postings per search term is plenty
DETAIL_CONCURRENCY = 5  # be a polite client; these are per-company hosts


@dataclass(frozen=True)
class Board:
    tenant: str
    shard: str
    site: str

    @property
    def api(self) -> str:
        return (
            f"https://{self.tenant}.{self.shard}.myworkdayjobs.com"
            f"/wday/cxs/{self.tenant}/{self.site}"
        )

    @classmethod
    def from_url(cls, url: str) -> Board | None:
        """Parse a pasted Workday careers URL.

        Accepts either of:
            https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
            https://nvidia.wd5.myworkdayjobs.com/en-US/Cisco_Careers/job/...
        """
        parsed = urlparse(url if "//" in url else f"https://{url}")
        host = re.match(
            r"^([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com$", parsed.netloc, re.I
        )
        if not host:
            return None

        segments = [s for s in parsed.path.split("/") if s]
        # Drop a leading locale segment such as "en-US".
        if segments and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segments[0]):
            segments = segments[1:]
        if not segments:
            return None

        return cls(tenant=host.group(1).lower(), shard=host.group(2).lower(),
                   site=segments[0])


# Verified live. Extend with `job-agent companies workday <url>`.
BOARDS: dict[str, Board] = {
    "nvidia": Board("nvidia", "wd5", "NVIDIAExternalCareerSite"),
    "salesforce": Board("salesforce", "wd12", "External_Career_Site"),
    "adobe": Board("adobe", "wd5", "external_experienced"),
    "workday": Board("workday", "wd5", "Workday"),
    "cisco": Board("cisco", "wd5", "Cisco_Careers"),
    "hp": Board("hp", "wd5", "ExternalCareerSite"),
    "micron": Board("micron", "wd1", "External"),
    "paypal": Board("paypal", "wd1", "jobs"),
}

USER_REGISTRY = "workday_boards.json"


def load_user_boards() -> None:
    """Merge tenants the user registered via `companies workday <url>`."""
    import json

    from ...config import data_dir

    path = data_dir() / USER_REGISTRY
    if not path.exists():
        return
    try:
        saved = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    for slug, spec in saved.items():
        try:
            BOARDS[slug] = Board(spec["tenant"], spec["shard"], spec["site"])
        except (KeyError, TypeError):
            continue


load_user_boards()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _list_page(
    client: httpx.AsyncClient, board: Board, offset: int, search: str
) -> tuple[list[dict], int]:
    try:
        response = await client.post(
            f"{board.api}/jobs",
            json={
                "appliedFacets": {},
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": search,
            },
        )
        if response.status_code != 200:
            return [], 0
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return [], 0
    return payload.get("jobPostings", []), int(payload.get("total", 0))


async def _hydrate(
    client: httpx.AsyncClient, board: Board, stub: dict, slug: str
) -> RawPosting | None:
    """Fetch the detail record; the list response has no description."""
    path = stub.get("externalPath") or ""
    detail = await get_json(client, f"{board.api}{path}")
    info = (detail or {}).get("jobPostingInfo") or {}

    title = (info.get("title") or stub.get("title") or "").strip()
    if not title:
        return None

    url = info.get("externalUrl") or (
        f"https://{board.tenant}.{board.shard}.myworkdayjobs.com"
        f"/{board.site}{path}"
    )

    return RawPosting(
        source=name,
        company_name=slug,
        title=title,
        url=url,
        location=(info.get("location") or stub.get("locationsText") or "").strip()
        or None,
        description=html_to_text(info.get("jobDescription")),
        # `postedOn` is prose ("Posted 7 Days Ago"); startDate is a real date.
        posted_at=_parse_date(info.get("startDate")),
        external_id=info.get("jobReqId") or info.get("jobPostingId"),
        ats=ATS.WORKDAY,
        ats_slug=slug,
    )


async def fetch(
    client: httpx.AsyncClient,
    slug: str,
    search: list[str] | None = None,
) -> list[RawPosting]:
    board = BOARDS.get(slug)
    if not board:
        return []

    # Without search terms a big tenant would mean thousands of detail calls.
    # Workday can filter server-side, so use it.
    terms = search or [""]
    stubs: dict[str, dict] = {}

    for term in terms:
        for page in range(MAX_PAGES):
            batch, total = await _list_page(client, board, page * PAGE_SIZE, term)
            if not batch:
                break
            for stub in batch:
                if path := stub.get("externalPath"):
                    stubs.setdefault(path, stub)
            if (page + 1) * PAGE_SIZE >= total:
                break

    semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def one(stub: dict) -> RawPosting | None:
        async with semaphore:
            try:
                return await _hydrate(client, board, stub, slug)
            except (httpx.HTTPError, ValueError):
                return None

    results = await asyncio.gather(*(one(s) for s in stubs.values()))
    return [r for r in results if r is not None]


async def probe(client: httpx.AsyncClient, slug: str) -> bool:
    board = BOARDS.get(slug)
    if not board:
        return False
    _, total = await _list_page(client, board, 0, "")
    return total > 0
