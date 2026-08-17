"""Paste a job URL, get the full pipeline.

Jobs come from everywhere — LinkedIn browsing, referrals, a friend's DM.
Scraping LinkedIn itself is off the table (their terms prohibit it and they
ban the accounts involved — hers is an asset this project must not risk),
but a posting she found by hand deserves the same treatment as a discovered
one: paste the application URL to the bot, the agent ingests, scores,
fills, and sends the review.

LinkedIn URLs are recognized and bounced with instructions to paste the
posting's actual Apply link instead — the ATS page is what can be filled.
"""

from __future__ import annotations

import re

import httpx

from .models import ATS, Job, RawPosting

URL = re.compile(r"^\s*(https?://\S+)\s*$", re.I)

_ATS_PATTERNS: list[tuple[re.Pattern, ATS]] = [
    (re.compile(r"//(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)", re.I),
     ATS.GREENHOUSE),
    (re.compile(r"//jobs\.lever\.co/([^/?#]+)", re.I), ATS.LEVER),
    (re.compile(r"//jobs\.ashbyhq\.com/([^/?#]+)", re.I), ATS.ASHBY),
    (re.compile(r"//jobs\.smartrecruiters\.com/([^/?#]+)", re.I),
     ATS.SMARTRECRUITERS),
    (re.compile(r"//[^/]*myworkday(?:jobs|site)\.com/", re.I), ATS.WORKDAY),
]

_LINKEDIN = re.compile(r"//(?:[a-z]+\.)?linkedin\.com/", re.I)

# "Senior Engineer - Acme Corp" / "Acme Corp - Senior Engineer" — page
# titles put the company on one side of a separator; the ATS slug decides
# which side we trust.
_TITLE_NOISE = re.compile(
    r"\s*[|–—-]\s*(?:careers?|jobs?|job application|apply|application)\b.*$",
    re.I,
)


def parse_url(text: str) -> str | None:
    if match := URL.match(text or ""):
        return match.group(1)
    return None


def is_linkedin(url: str) -> bool:
    return bool(_LINKEDIN.search(url))


def detect(url: str) -> tuple[ATS, str]:
    """(ats, company slug) from the URL shape. UNKNOWN + host otherwise."""
    for pattern, ats in _ATS_PATTERNS:
        if match := pattern.search(url):
            slug = match.group(1) if match.groups() else ""
            return ats, slug
    host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url).split("/")[0])
    return ATS.UNKNOWN, host.split(".")[0]


def page_title(url: str) -> str:
    """The <title> of the posting, as a fallback job title."""
    try:
        r = httpx.get(
            url, timeout=15.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (job-agent paste)"},
        )
        if r.status_code != 200:
            return ""
        if match := re.search(r"<title[^>]*>(.*?)</title>", r.text,
                              re.I | re.S):
            title = re.sub(r"\s+", " ", match.group(1)).strip()
            return _TITLE_NOISE.sub("", title)[:120]
    except httpx.HTTPError:
        pass
    return ""


def build_job(url: str, profile, resumes) -> Job | None:
    """A scored Job from a pasted URL, or None if nothing could be read."""
    from .match.score import score_job

    ats, slug = detect(url)
    title = page_title(url)
    if not title:
        return None
    company = slug.replace("-", " ").strip() or "unknown"
    # Ashby/Greenhouse titles read "Job Title - Company" or "Company - Job
    # Title"; strip the company token wherever it sits.
    cleaned = re.sub(re.escape(company), "", title, flags=re.I).strip(" -–—|·")
    posting = RawPosting(
        source="paste", company_name=company,
        title=cleaned or title, url=url, ats=ats,
    )
    job = Job.from_posting(posting)
    job.match_score, job.match_breakdown, job.best_resume = score_job(
        job, profile, resumes
    )
    return job
