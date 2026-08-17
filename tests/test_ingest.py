"""Pasted-URL ingestion: URL shapes, ATS detection, LinkedIn bounce."""

from __future__ import annotations

import pytest

from job_agent import ingest
from job_agent.models import ATS


@pytest.mark.parametrize(("text", "expected"), [
    ("https://jobs.lever.co/zoox/abc123/apply", "https://jobs.lever.co/zoox/abc123/apply"),
    ("  https://a.co/x  ", "https://a.co/x"),
    ("check this https://a.co/x out", None),   # prose, not a paste
    ("12 auto", None),
    ("", None),
])
def test_parse_url(text, expected) -> None:
    assert ingest.parse_url(text) == expected


@pytest.mark.parametrize(("url", "ats", "slug"), [
    ("https://job-boards.greenhouse.io/stripe/jobs/123", ATS.GREENHOUSE, "stripe"),
    ("https://boards.greenhouse.io/robinhood/jobs/1?x=1", ATS.GREENHOUSE, "robinhood"),
    ("https://jobs.lever.co/zoox/uuid/apply", ATS.LEVER, "zoox"),
    ("https://jobs.ashbyhq.com/openai/uuid/application", ATS.ASHBY, "openai"),
    ("https://nvidia.wd5.myworkdayjobs.com/Careers/job/x", ATS.WORKDAY, ""),
    ("https://careers.example.com/apply/42", ATS.UNKNOWN, "careers"),
])
def test_ats_detection(url, ats, slug) -> None:
    got_ats, got_slug = ingest.detect(url)
    assert got_ats is ats
    if slug:
        assert got_slug == slug


def test_linkedin_is_recognized_and_refused() -> None:
    assert ingest.is_linkedin("https://www.linkedin.com/jobs/view/123")
    assert ingest.is_linkedin("https://linkedin.com/jobs/view/123")
    assert not ingest.is_linkedin("https://jobs.lever.co/zoox/x")
