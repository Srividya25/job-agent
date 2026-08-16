"""The title-relevance gate: her list is for engineering roles only.

Born from a real complaint: "Customer Experience Representative" rode a 14%
score onto a 252-job proposal. Match scores rank; this gate decides what is
even eligible.
"""

from __future__ import annotations

import pytest

from job_agent.config import Identity, Preferences, Profile
from job_agent.match.score import passes_gates
from job_agent.models import ATS, Job


def profile() -> Profile:
    return Profile(
        identity=Identity(first_name="Jane", last_name="Doe", email="j@e.co"),
        preferences=Preferences(titles=[
            "Software Engineer", "Software Developer", "Backend Engineer",
            "Machine Learning Engineer", "AI Engineer",
        ]),
    )


def job(title: str) -> Job:
    return Job(
        dedupe_key=f"k-{title[:20]}", company="Acme", title=title,
        url="https://example.com/x", ats=ATS.GREENHOUSE,
    )


@pytest.mark.parametrize("title", [
    "Customer Experience Representative, Executive Office",
    "Technical Recruiter, Infrastructure",
    "PM Administrative Assistant",
    "Service Parts Advisor, Costa Mesa",
    "Creative Producer, Experiential",
    "Mobile Service Technician, Millbrae",
])
def test_unrelated_titles_are_gated(title: str) -> None:
    ok, why = passes_gates(job(title), profile())
    assert not ok
    assert "unrelated" in why


@pytest.mark.parametrize("title", [
    "Software Engineer, New Grad (Dec 2026)",
    "Sr Software Development Engineer",       # Workday's phrasing
    "Backend Engineer, Credit Decisions",
    "Machine Learning Engineer, Ranking",
    "Software Developer",
    "Full Stack Software Engineer",
])
def test_engineering_titles_pass(title: str) -> None:
    ok, why = passes_gates(job(title), profile())
    assert ok, f"{title!r} was gated: {why}"


def test_gate_can_be_disabled() -> None:
    p = profile()
    p.preferences.min_title_match = 0.0
    ok, _ = passes_gates(job("Customer Experience Representative"), p)
    assert ok
