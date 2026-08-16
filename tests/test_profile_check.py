"""The profile/resume consistency check.

Built after a real failure: profile.yaml carried "Santa Clara University",
migrated from hardcoded legacy code, while every resume named a different
school. Nothing compared the two, so a fabricated school was filled into
every form. (All names and dates here are fictional test data.)
"""

from __future__ import annotations

import pytest

from job_agent.config import Education, Experience, Identity, Profile, Skills
from job_agent.match.resume import ParsedResume
from job_agent.profile_check import Level, check_resume

RESUME_TEXT = """
Jane Doe
jane@example.com | 555-123-4567

EDUCATION
Bayview State University Springfield, VA
Master of Science in Software Engineering | GPA: 3.7/4.0 Aug 2019 - May 2022
Lakeshore University Portland, OR
Bachelor of Engineering in Computer Science Aug 2013 - May 2017

TECHNICAL SKILLS
Python, Java, SQL, React, AWS
"""


def resume() -> ParsedResume:
    return ParsedResume(label="test", path="x.pdf", text=RESUME_TEXT)


def make_profile(**kw) -> Profile:
    base = dict(
        identity=Identity(first_name="Jane", last_name="Doe",
                          email="jane@example.com", phone="5551234567"),
        education=[Education(school="Bayview State University",
                             degree="Master of Science",
                             field="Software Engineering", graduation_year=2022)],
        experience=Experience(years=1),
        skills=Skills(must_have=["Python"]),
    )
    base.update(kw)
    return Profile(**base)


def errors(findings) -> list[str]:
    return [f.field for f in findings if f.level is Level.ERROR]


def test_consistent_profile_has_no_errors() -> None:
    assert errors(check_resume(make_profile(), resume())) == []


def test_catches_a_school_not_on_the_resume() -> None:
    """The exact bug this module exists for."""
    p = make_profile(education=[Education(
        school="Santa Clara University", degree="Bachelor's",
        field="Computer Science", graduation_year=2023)])
    found = errors(check_resume(p, resume()))
    assert "education[0].school" in found


def test_catches_a_masters_the_profile_omits() -> None:
    """Rules answer from education[0]; omitting the MS answers with the BE."""
    p = make_profile(education=[Education(
        school="Lakeshore University", degree="Bachelor of Engineering",
        field="Computer Science", graduation_year=2017)])
    assert "education" in errors(check_resume(p, resume()))


def test_catches_a_wrong_email() -> None:
    """A mismatch points the application somewhere the resume does not."""
    p = make_profile(identity=Identity(
        first_name="Jane", last_name="Doe", email="typo@example.com"))
    assert "identity.email" in errors(check_resume(p, resume()))


def test_catches_a_wrong_name() -> None:
    p = make_profile(identity=Identity(
        first_name="Jane", last_name="Smith", email="jane@example.com"))
    assert "identity.last_name" in errors(check_resume(p, resume()))


def test_warns_on_experience_beyond_graduation() -> None:
    p = make_profile(experience=Experience(years=12))
    warns = [f.field for f in check_resume(p, resume()) if f.level is Level.WARN]
    assert "experience.years" in warns


def test_warns_on_a_must_have_skill_missing_from_the_resume() -> None:
    p = make_profile(skills=Skills(must_have=["Rust"]))
    warns = [f.field for f in check_resume(p, resume()) if f.level is Level.WARN]
    assert "skills.must_have" in warns
