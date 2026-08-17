"""The Adzuna aggregator lane: payload parsing, no network."""

from __future__ import annotations

from job_agent.discover.sources.adzuna import parse_results

PAYLOAD = {
    "results": [
        {
            "id": 12345,
            "title": "Software <strong>Engineer</strong>",
            "company": {"display_name": "Acme Robotics"},
            "location": {"display_name": "San Jose, CA"},
            "description": "Build robots with Python.",
            "created": "2026-08-17T09:30:00Z",
            "redirect_url": "https://www.adzuna.com/land/ad/12345",
        },
        {   # missing company — dropped, not crashed
            "id": 2, "title": "Ghost Role",
            "redirect_url": "https://x.co/2", "company": {},
        },
        {   # missing url — dropped
            "id": 3, "title": "No Link", "company": {"display_name": "X"},
        },
    ]
}


def test_parse_results() -> None:
    postings = parse_results(PAYLOAD)
    assert len(postings) == 1
    p = postings[0]
    assert p.company_name == "Acme Robotics"
    assert p.title == "Software Engineer"          # highlight tags stripped
    assert p.source == "adzuna"
    assert p.posted_at is not None and p.posted_at.day == 17
    assert p.external_id == "12345"


def test_empty_payload() -> None:
    assert parse_results({}) == []
    assert parse_results({"results": None}) == []
