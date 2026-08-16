"""Tiered resolution against real forms."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from job_agent.config import (
    Address,
    EEO,
    Education,
    Experience,
    Identity,
    Preferences,
    Profile,
    WorkAuthorization,
)
from job_agent.forms.extract import FormField, extract_fields
from job_agent.resolve import cache, rules
from job_agent.resolve.engine import Tier, resolve_fields

FIXTURES = Path(__file__).parent / "fixtures" / "pages"


@pytest.fixture
def profile() -> Profile:
    """A synthetic profile — never the real one, so tests stay shareable."""
    return Profile(
        identity=Identity(
            first_name="Jane", last_name="Doe", email="jane@example.com",
            phone="5550001111", linkedin="https://linkedin.com/in/janedoe",
            github="https://github.com/janedoe", portfolio="https://jane.dev",
        ),
        address=Address(line1="1 Test St", city="Springfield", state="Illinois",
                        postal_code="62701", country="United States"),
        work_authorization=WorkAuthorization(
            status="F-1 STEM OPT", needs_sponsorship=True, authorized_now=True
        ),
        education=[Education(school="Example University", degree="Bachelor's",
                             field="Computer Science", graduation_year=2023,
                             start_month="September", start_year=2019,
                             end_month="June")],
        experience=Experience(years=2, current_title="Software Engineer"),
        preferences=Preferences(),
        eeo=EEO(gender="Female", race="Asian"),
    )


def field(label: str, type_: str = "text", **kw) -> FormField:
    return FormField(ref=f"#{label}", label=label, type=type_, **kw)


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("First Name", "Jane"),
        ("Last Name", "Doe"),
        ("Full Name", "Jane Doe"),
        ("Email", "jane@example.com"),
        ("Phone", "5550001111"),
        ("LinkedIn Profile", "https://linkedin.com/in/janedoe"),
        ("GitHub URL", "https://github.com/janedoe"),
        ("Portfolio", "https://jane.dev"),
        ("City", "Springfield"),
        ("State", "Illinois"),
        ("Zip Code", "62701"),
        ("University", "Example University"),
        ("Gender", "Female"),
    ],
)
def test_rule_resolution(profile: Profile, label: str, expected: str) -> None:
    assert rules.resolve(field(label), profile) == expected


def test_surname_beats_generic_name(profile: Profile) -> None:
    """'Last name' contains 'name'; ordering must not let the generic rule win."""
    assert rules.resolve(field("Last Name"), profile) == "Doe"
    assert rules.resolve(field("First Name"), profile) == "Jane"


def test_portfolio_does_not_capture_linkedin(profile: Profile) -> None:
    """'LinkedIn Profile' contains 'profile' — the link rules must not collide."""
    assert rules.resolve(field("LinkedIn Profile"), profile) == profile.identity.linkedin


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Are you legally authorized to work in the United States?", "Yes"),
        ("Do you now or will you in the future require visa sponsorship?", "Yes"),
        ("Will you require sponsorship for employment visa status?", "Yes"),
    ],
)
def test_work_authorization_opposite_phrasings(
    profile: Profile, label: str, expected: str
) -> None:
    """The same fact is asked two opposite ways; both must answer correctly."""
    assert rules.resolve(field(label), profile) == expected


def test_sponsorship_answer_follows_profile() -> None:
    """A candidate who needs no sponsorship must answer 'No' to the same question."""
    citizen = Profile(
        identity=Identity(first_name="A", last_name="B", email="a@b.com"),
        work_authorization=WorkAuthorization(
            needs_sponsorship=False, authorized_now=True
        ),
    )
    label = "Will you now or in the future require visa sponsorship?"
    assert rules.resolve(field(label), citizen) == "No"


def test_unknown_label_returns_none(profile: Profile) -> None:
    assert rules.resolve(field("What is your favourite algorithm?"), profile) is None


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Why do you want to work here? *", "Why do you want to work here?"),
        ("  Why do you WANT to work here  ", "why do you want to work here"),
        ("Email (required)", "Email"),
        ("Please describe your experience.", "Describe your experience"),
    ],
)
def test_equivalent_questions_share_a_key(a: str, b: str) -> None:
    assert cache.question_hash(a) == cache.question_hash(b)


def test_different_questions_differ() -> None:
    assert cache.question_hash("First name") != cache.question_hash("Last name")


# --------------------------------------------------------------------------
# end to end on real forms
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["greenhouse_airbnb", "lever_spotify"])
def test_identity_fields_resolve_on_real_forms(
    profile: Profile, fixture: str
) -> None:
    fields = extract_fields((FIXTURES / f"{fixture}.html").read_text())
    resolution = resolve_fields(fields, profile, use_cache=False)
    answered = {a.label.lower(): a.value for a in resolution.answers}
    joined = " ".join(answered)

    assert any("email" in k for k in answered), "email unresolved"
    assert any("phone" in k for k in answered), "phone unresolved"
    assert any("name" in k for k in joined.split()) or any(
        "name" in k for k in answered
    )
    # Nothing may be answered with a value the profile never supplied.
    allowed = {
        profile.identity.first_name, profile.identity.last_name,
        profile.identity.full_name, profile.identity.email,
        profile.identity.phone, profile.identity.linkedin,
        profile.identity.github, profile.identity.portfolio,
        profile.address.line1, profile.address.line2, profile.address.city,
        profile.address.state, profile.address.postal_code,
        profile.address.country, f"{profile.address.city}, {profile.address.state}",
        "Example University", "Bachelor's", "Computer Science", "2023",
        "Software Engineer", "2", "Yes", "No", "Female", "Asian", "",
    }
    for answer in resolution.answers:
        assert answer.value in allowed, (
            f"invented value {answer.value!r} for {answer.label!r}"
        )


def test_resume_upload_resolves(profile: Profile) -> None:
    """Every application needs the resume attached; it must never be missed."""
    resume = FormField(ref="#resume", label="resume", type="file")
    assert rules.resolve(resume, profile, resume_path="profile/cv.pdf") == "profile/cv.pdf"

    # Unlabelled file inputs still get the resume — it is the safe default.
    bare = FormField(ref="#f", label="", type="file")
    assert rules.resolve(bare, profile, resume_path="profile/cv.pdf") == "profile/cv.pdf"


def test_cover_letter_is_not_given_the_resume(profile: Profile) -> None:
    """Uploading a resume where a cover letter is asked for is worse than blank."""
    cover = FormField(ref="#cl", label="Cover Letter", type="file")
    assert rules.resolve(cover, profile, resume_path="profile/cv.pdf") == ""


def test_generic_fields_fully_resolved_on_real_forms(profile: Profile) -> None:
    """Rules cannot answer company-specific questions, but must answer every
    generic one. This is the criterion that actually matters — not raw
    percentage, which is bounded by how many bespoke questions a form asks."""
    from job_agent.forms.extract import extract_fields as _extract

    generic = re.compile(
        r"\b(first|last|full)\s*name\b|\be-?mail\b|\bphone\b|\blinked-?in\b|"
        r"\bgit-?hub\b|\bportfolio\b|\bcity\b|\bstate\b|\bcountry\b",
        re.I,
    )
    for fixture in ("greenhouse_airbnb", "lever_spotify", "greenhouse_instacart"):
        fields = _extract((FIXTURES / f"{fixture}.html").read_text())
        resolution = resolve_fields(fields, profile, use_cache=False)
        missed = [
            f.label for f in resolution.unresolved if generic.search(f.label or "")
        ]
        assert missed == [], f"{fixture}: generic fields unresolved: {missed}"


def test_all_rule_answers_are_confident(profile: Profile) -> None:
    """Tier <= 2 is what the auto-submit gate depends on."""
    fields = extract_fields((FIXTURES / "greenhouse_airbnb.html").read_text())
    resolution = resolve_fields(fields, profile, use_cache=False)
    assert resolution.answers, "nothing resolved at all"
    assert all(a.tier is Tier.RULES for a in resolution.answers)
    assert resolution.all_confident


# --------------------------------------------------------------------------
# bot traps
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "Enter website. This input is for robots only, please leave blank",
        "Leave this field blank",
        "Please leave empty",
        "Do not fill this in",
        "If you are human, leave this blank",
    ],
)
def test_honeypot_is_left_blank(label: str, profile) -> None:
    """Anything written into a decoy marks the application as automated.

    The real one: Salesforce's Workday form carries a "website" honeypot, and
    the portfolio rule matched it and filled in the URL.
    """
    from job_agent.resolve.rules import resolve

    field = FormField(ref="#hp", label=label, type="text")
    assert resolve(field, profile) == ""


def test_honeypot_named_resume_gets_no_file(profile) -> None:
    from job_agent.resolve.rules import resolve

    field = FormField(ref="#hp", label="Resume — robots only", type="file")
    assert resolve(field, profile, "profile/resume_general.pdf") == ""


def test_a_real_website_field_still_resolves(profile) -> None:
    from job_agent.resolve.rules import resolve

    field = FormField(ref="#w", label="Website", type="url")
    assert resolve(field, profile).startswith("http")


def test_office_choice_is_not_the_city_field(profile) -> None:
    """Plaid's "New York City Office" was filled with "Springfield".

    The label contains "City" but is asking which office she wants, not where
    she lives. The review message caught it — this stops it being written at
    all.
    """
    from job_agent.resolve.rules import resolve

    for label in ("New York City Office", "San Francisco Office", "Office location"):
        assert not resolve(FormField(ref="#o", label=label, type="text"), profile)


def test_a_real_city_field_still_resolves(profile) -> None:
    from job_agent.resolve.rules import resolve

    for label in ("City", "Current City", "Location (City)", "City/Town"):
        assert resolve(FormField(ref="#c", label=label, type="text"), profile) == "Springfield"


# --------------------------------------------------------------------------
# education dates split across two controls
# --------------------------------------------------------------------------


MONTHS = ["Month...", "January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]
YEARS = ["Year..."] + [str(y) for y in range(1950, 2031)]


@pytest.mark.parametrize(
    ("label", "options", "expected"),
    [
        ("Start Date", MONTHS, "September"),
        ("Start Date", YEARS, "2019"),
        ("End Date", MONTHS, "June"),
        ("End Date", YEARS, "2023"),
        ("Graduation Date", YEARS, "2023"),
    ],
)
def test_month_and_year_halves_get_different_answers(
    label: str, options: list[str], expected: str, profile
) -> None:
    """The two selects share one label, so only the options tell them apart.

    Both used to receive the same answer, which put "May" into a list of
    years and left the graduation year off the application entirely.
    """
    from job_agent.resolve.rules import resolve

    field = FormField(ref="#d", label=label, type="select", options=options)
    assert resolve(field, profile) == expected


def test_a_date_control_with_no_options_is_left_alone(profile) -> None:
    """Nothing to disambiguate on, so do not guess a half."""
    from job_agent.resolve.rules import resolve

    field = FormField(ref="#d", label="Start Date", type="text")
    assert resolve(field, profile) is None


def test_a_non_date_select_is_unaffected(profile) -> None:
    from job_agent.resolve.rules import resolve

    field = FormField(ref="#s", label="State", type="select",
                      options=["Illinois", "Texas"])
    assert resolve(field, profile) == "Illinois"
