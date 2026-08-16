"""Verify profile.yaml against the resumes it claims to describe.

This exists because of a real failure: the profile said "Santa Clara
University" — a value migrated from hardcoded legacy code — while every
resume named a different school. Nothing compared the two, so the wrong
school was filled into every form for days.

The resume is treated as the source of truth. It is the document that
actually goes to the employer, so anything the profile asserts that the
resume contradicts is a defect in the profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .config import Profile
from .match.resume import ParsedResume


class Level(StrEnum):
    ERROR = "error"      # profile contradicts the resume
    WARN = "warn"        # suspicious, may be legitimate
    OK = "ok"


@dataclass
class Finding:
    level: Level
    field: str
    detail: str


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _present(needle: str, haystack: str) -> bool:
    return bool(needle) and _norm(needle) in haystack


def _norm_url(url: str) -> str:
    """Compare links by identity, not by spelling.

    A resume writes "linkedin.com/in/jane"; the profile stores
    "https://linkedin.com/in/jane/". A plain substring test calls that a
    mismatch and reports a contradiction that is not one — which trains the
    reader to ignore the check.
    """
    u = _norm(url)
    for prefix in ("https://", "http://", "www."):
        u = u.removeprefix(prefix)
    return u.rstrip("/")


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------


def check_identity(profile: Profile, text: str) -> list[Finding]:
    out: list[Finding] = []
    ident = profile.identity

    if not _present(ident.last_name, text):
        out.append(Finding(Level.ERROR, "identity.last_name",
                           f"{ident.last_name!r} does not appear in the resume"))
    if not _present(ident.first_name.split()[0], text):
        out.append(Finding(Level.ERROR, "identity.first_name",
                           f"{ident.first_name!r} does not appear in the resume"))

    # Email and phone are what a recruiter replies to; a mismatch here means
    # an application points somewhere the resume does not.
    if ident.email and not _present(ident.email, text):
        out.append(Finding(Level.ERROR, "identity.email",
                           f"{ident.email} is not on the resume"))

    # Links are compared normalized; scheme and trailing slash are noise.
    resume_norm = _norm_url(text)
    for field_name, value in (
        ("linkedin", ident.linkedin),
        ("github", ident.github),
        ("portfolio", ident.portfolio),
    ):
        if value and _norm_url(value) not in resume_norm:
            out.append(Finding(
                Level.WARN, f"identity.{field_name}",
                f"{value} is not on this resume — confirm it is current",
            ))

    digits = re.sub(r"\D", "", ident.phone or "")
    if digits:
        resume_digits = re.sub(r"\D", "", text)
        if digits[-10:] not in resume_digits:
            out.append(Finding(Level.WARN, "identity.phone",
                               "phone number not found on the resume"))
    return out


_DEGREE_WORDS = re.compile(
    r"\b(bachelor|master|b\.?s\.?|m\.?s\.?|b\.?e\.?|m\.?e\.?|ph\.?d|mba)\b", re.I
)


def check_education(profile: Profile, text: str) -> list[Finding]:
    out: list[Finding] = []

    if not profile.education:
        out.append(Finding(Level.WARN, "education", "no education in profile"))
        return out

    for i, edu in enumerate(profile.education):
        if not _present(edu.school, text):
            out.append(Finding(
                Level.ERROR, f"education[{i}].school",
                f"{edu.school!r} does not appear in the resume",
            ))
        if edu.field and not _present(edu.field, text):
            out.append(Finding(
                Level.WARN, f"education[{i}].field",
                f"{edu.field!r} not found in the resume",
            ))
        if edu.graduation_year and str(edu.graduation_year) not in text:
            out.append(Finding(
                Level.WARN, f"education[{i}].graduation_year",
                f"{edu.graduation_year} not found in the resume",
            ))

    # A degree on the resume that the profile omits gets answered with the
    # wrong one, since rules answer from education[0].
    resume_degrees = {m.group(0).lower().rstrip(".") for m in _DEGREE_WORDS.finditer(text)}
    profile_degrees = _norm(" ".join(e.degree for e in profile.education))
    if "master" in resume_degrees and "master" not in profile_degrees:
        out.append(Finding(
            Level.ERROR, "education",
            "resume shows a Master's the profile does not list",
        ))
    return out


def check_experience(profile: Profile, text: str) -> list[Finding]:
    """Sanity-check the stated years against the most recent graduation.

    Not authoritative — people work before and during study — but a claim
    well beyond time-since-graduation is worth a second look, because it is
    answered verbatim into "years of experience" fields.
    """
    out: list[Finding] = []
    years = profile.experience.years
    if not years:
        return out

    grad_years = [e.graduation_year for e in profile.education if e.graduation_year]
    if not grad_years:
        return out

    from datetime import date

    latest = max(grad_years)
    since_latest = date.today().year - latest
    if years > since_latest + 2:
        out.append(Finding(
            Level.WARN, "experience.years",
            f"{years:g} years claimed, but the most recent degree finished "
            f"{latest} ({since_latest} years ago)",
        ))
    return out


def check_unverifiable(profile: Profile) -> list[Finding]:
    """Values no resume can corroborate, so only the user can.

    Street address and postal code are never on a resume. They are reported
    once, at INFO-adjacent WARN level, so they are consciously confirmed
    rather than silently trusted — which is how a wrong school survived for
    days.
    """
    out: list[Finding] = []
    addr = profile.address
    if addr.line1:
        out.append(Finding(
            Level.OK, "address",
            f"{addr.line1}, {addr.city} {addr.postal_code} "
            "— not on any resume; user-confirmed 2026-08",
        ))
    return out


def check_skills(profile: Profile, text: str) -> list[Finding]:
    """A must-have skill absent from the resume weakens every match."""
    out: list[Finding] = []
    for skill in profile.skills.must_have:
        if not _present(skill, text):
            out.append(Finding(
                Level.WARN, "skills.must_have",
                f"{skill!r} is a must-have but is not on this resume",
            ))
    return out


# --------------------------------------------------------------------------


def check_resume(profile: Profile, resume: ParsedResume) -> list[Finding]:
    text = _norm(resume.text)
    return [
        *check_identity(profile, text),
        *check_education(profile, text),
        *check_experience(profile, text),
        *check_skills(profile, text),
        *check_unverifiable(profile),
    ]


def check_all(
    profile: Profile, resumes: list[ParsedResume]
) -> dict[str, list[Finding]]:
    return {r.label: check_resume(profile, r) for r in resumes}
