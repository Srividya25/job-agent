"""Save real application forms as test fixtures.

Greenhouse and Lever render their forms server-side, so fixtures can be
captured over plain HTTP — no browser, no login. Ashby and Workday are
client-rendered and will need a live page (Phase 3).

Everything captured is scrubbed before it touches disk. A raw application
page carries CSRF tokens, session ids and sometimes prefilled personal data,
and these fixtures are destined for a public repository.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from ..config import ROOT

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "pages"

# Greenhouse's embedded application form; `token` is the job id.
GREENHOUSE_APP = "https://boards.greenhouse.io/embed/job_app?token={job_id}"

# --------------------------------------------------------------------------
# scrubbing
#
# Ordered most-specific first. Each pattern keeps the surrounding markup
# intact so the DOM structure — the thing under test — is unchanged.
# --------------------------------------------------------------------------

_SCRUB: list[tuple[re.Pattern[str], str]] = [
    # CSRF / auth tokens in attributes or inline JSON
    (re.compile(r'(name=["\'](?:authenticity_token|csrf_token|_token)["\']\s+'
                r'value=["\'])[^"\']+', re.I), r"\1SCRUBBED"),
    (re.compile(r'("(?:csrf|authenticity|session|api|access)[_-]?'
                r'(?:token|key|id)"\s*:\s*")[^"]+', re.I), r"\1SCRUBBED"),
    (re.compile(r'(<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\'])[^"\']+',
                re.I), r"\1SCRUBBED"),
    # cookies and bearer tokens anywhere
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1SCRUBBED"),
    # email addresses (prefilled values, recruiter contacts)
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
     "scrubbed@example.com"),
    # US phone numbers
    (re.compile(r"\b(?:\+?1[\s.\-])?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),
     "555-555-5555"),
]


def scrub(html: str) -> str:
    for pattern, replacement in _SCRUB:
        html = pattern.sub(replacement, html)
    return html


def fixture_path(name: str) -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    return FIXTURE_DIR / f"{name}.html"


def save_fixture(name: str, html: str) -> Path:
    path = fixture_path(name)
    path.write_text(scrub(html), encoding="utf-8")
    return path


def capture_greenhouse(job_id: str, name: str) -> Path:
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(GREENHOUSE_APP.format(job_id=job_id))
        response.raise_for_status()
    return save_fixture(name, response.text)


def capture_url(url: str, name: str) -> Path:
    """Capture any server-rendered application page."""
    with httpx.Client(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; job-agent/0.1)"},
    ) as client:
        response = client.get(url)
        response.raise_for_status()
    return save_fixture(name, response.text)
