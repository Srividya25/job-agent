"""Extract the years-of-experience bar from a job description.

Title-based seniority filtering only catches roles that say "Senior". Plenty
of postings titled plain "Software Engineer" quietly require five years, and
they are the ones worth not wasting an application on.

The rule applied here: take the LOWEST hard requirement stated. Postings
routinely list a floor plus aspirational extras — "2+ years backend, 5+ years
distributed systems preferred" — and the floor is the real bar. Taking the
maximum would reject roles that are genuinely open.
"""

from __future__ import annotations

import re

# A number of years, sitting near the word "experience". The proximity
# requirement matters: descriptions are full of unrelated year counts
# ("over 10 years serving customers", "founded 5 years ago").
_PATTERNS = [
    # "3-5 years of experience", "3 to 5 years experience"
    re.compile(
        r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\+?\s*years?[^.]{0,40}?\bexperience",
        re.I,
    ),
    # "5+ years of experience", "5 years experience"
    re.compile(r"(\d{1,2})\+?\s*years?[^.]{0,40}?\bexperience", re.I),
    # "experience: 5+ years", "experience of at least 3 years"
    re.compile(r"experience[^.]{0,40}?(\d{1,2})\+?\s*years?", re.I),
    # "at least 3 years", "minimum of 3 years"
    re.compile(r"(?:at least|minimum(?: of)?|min\.?)\s*(\d{1,2})\+?\s*years?", re.I),
]

# Requirements attached to a *different* thing than the role's own bar.
# "PhD plus 2 years" or "Master's with 1 year" describe an alternative path,
# and "5 years of company growth" is not a requirement at all.
_IGNORE_CONTEXT = re.compile(
    r"(founded|growth|history|anniversary|over the (past|last)|in business|"
    r"serving|since \d{4})",
    re.I,
)

MAX_PLAUSIBLE_YEARS = 20


def extract_min_years(description: str | None) -> int | None:
    """Lowest stated experience requirement, or None if the JD does not say."""
    if not description:
        return None

    found: list[int] = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(description):
            window = description[max(0, match.start() - 60) : match.end() + 60]
            if _IGNORE_CONTEXT.search(window):
                continue
            # A range contributes its lower bound.
            groups = [g for g in match.groups() if g]
            try:
                value = min(int(g) for g in groups)
            except ValueError:
                continue
            if 0 <= value <= MAX_PLAUSIBLE_YEARS:
                found.append(value)

    return min(found) if found else None


def passes_experience_bar(
    description: str | None, max_years: int
) -> tuple[bool, str]:
    """Whether the posting's floor is within reach.

    A posting that states no requirement passes. Silence is common on new-grad
    and mid-level roles, and rejecting on absence would discard many of the
    best matches.
    """
    if max_years <= 0:
        return True, ""

    years = extract_min_years(description)
    if years is None:
        return True, ""
    if years > max_years:
        return False, f"requires {years}+ years experience"
    return True, ""
