"""Field extraction against real captured application forms.

These run in about a second with no browser, no network and no login. That is
the whole point of Phase 2: the resolver can be iterated on in a fast loop
instead of by relaunching Chrome and clicking through three pages.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from job_agent.forms.extract import (
    FormField,
    _humanize,
    _normalize_label,
    extract_fields,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pages"


def load(name: str) -> list[FormField]:
    return extract_fields((FIXTURES / f"{name}.html").read_text())


def labels(fields: list[FormField]) -> list[str]:
    return [f.label for f in fields if f.label]


def find(fields: list[FormField], text: str) -> FormField | None:
    return next((f for f in fields if text.lower() in f.label.lower()), None)


# --------------------------------------------------------------------------
# label normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("First Name *", "First Name"),
        ("Email (required)", "Email"),
        ("  Phone   Number  ", "Phone Number"),
        # Inline validation text must not become part of the label.
        ("Current location ✱ No location found. Try again", "Current location"),
        ("Resume/CV ✱ ATTACH RESUME/CV Couldn't auto-read", "Resume/CV"),
        # Pure placeholders are not labels.
        ("Select...", ""),
        ("--", ""),
        ("Attach", ""),
    ],
)
def test_normalize_label(raw: str, expected: str) -> None:
    assert _normalize_label(raw) == expected


def test_humanize_machine_names() -> None:
    assert _humanize("first_name") == "first name"
    assert _humanize("candidateFirstName") == "candidate first name"
    assert _humanize("answers[0][text]") == "answers text"


# --------------------------------------------------------------------------
# real forms
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["greenhouse_airbnb", "lever_spotify"])
def test_identity_fields_always_resolve(fixture: str) -> None:
    """Zero tolerance on identity fields — these must never be missed."""
    found = labels(load(fixture))
    joined = " | ".join(found).lower()
    assert "email" in joined
    assert "phone" in joined
    assert "name" in joined


def test_greenhouse_core_fields() -> None:
    fields = load("greenhouse_airbnb")

    first = find(fields, "First Name")
    assert first is not None
    assert first.type == "text"
    assert first.required
    assert first.ref == "#first_name"

    email = find(fields, "Email")
    assert email is not None and email.ref == "#email"

    # File inputs carry no name attribute and their only nearby text is the
    # "Attach" button caption, so the id is what identifies them.
    resume = next(f for f in fields if f.ref == "#resume")
    assert resume.type == "file"
    assert resume.label == "resume"


def test_lever_all_fields_labelled() -> None:
    fields = load("lever_spotify")
    unlabelled = [f for f in fields if not f.label]
    assert unlabelled == [], f"unlabelled: {[f.ref for f in unlabelled]}"


def test_lever_link_fields_distinguished() -> None:
    """LinkedIn/GitHub/Portfolio share a name prefix and must stay distinct."""
    fields = load("lever_spotify")
    for wanted in ("LinkedIn", "GitHub", "Portfolio"):
        field = find(fields, wanted)
        assert field is not None, f"missing {wanted}"
        assert field.type == "text"


def test_checkbox_group_collapses_to_one_field() -> None:
    """Ten pronoun checkboxes are one question, not ten."""
    fields = load("lever_spotify")
    pronouns = find(fields, "Pronouns")
    assert pronouns is not None
    assert pronouns.type == "checkbox"
    assert len(pronouns.options) > 3
    assert "She/her" in pronouns.options
    # And the group is named for the question, not the first option.
    assert pronouns.label != "He/him"


def test_refs_are_unique() -> None:
    """A duplicate ref would fill the wrong control in Phase 3."""
    for fixture in ("greenhouse_airbnb", "lever_spotify", "greenhouse_instacart"):
        refs = [f.ref for f in load(fixture)]
        assert len(refs) == len(set(refs)), f"{fixture} has duplicate refs"


def test_validation_shims_excluded() -> None:
    """React-Select renders a hidden required-input beside each dropdown.

    Greenhouse's Airbnb form carries six. They have no name, no id and no
    label, so they are phantom duplicates of the styled control next to them
    and must not appear as fields.
    """
    fields = load("greenhouse_airbnb")
    phantoms = [f for f in fields if not f.label and f.ref.startswith("input:nth")]
    assert phantoms == [], f"validation shims leaked through: {phantoms}"


def test_every_field_is_referenceable() -> None:
    """A field we cannot address is a field we cannot fill."""
    for fixture in ("greenhouse_airbnb", "lever_spotify", "greenhouse_instacart"):
        for field in load(fixture):
            assert field.ref, f"{fixture}: field with empty ref"
            assert field.label or field.type == "file", (
                f"{fixture}: unlabelled non-file field {field.ref}"
            )


def test_submit_buttons_excluded() -> None:
    for fixture in ("greenhouse_airbnb", "lever_spotify"):
        assert all(f.type != "submit" for f in load(fixture))


def test_no_secrets_in_fixtures() -> None:
    """Fixtures are committed, so scrubbing must have worked.

    Checks patterns rather than literal values — asserting that a specific
    phone number is absent would put that phone number in a public repo.
    """
    import re

    real_email = re.compile(
        r"[\w.%+-]+@(?!example\.com)[\w.-]+\.[A-Za-z]{2,}"
    )
    real_phone = re.compile(
        r"\b(?:\+?1[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"
    )

    for path in FIXTURES.glob("*.html"):
        text = path.read_text()

        if "authenticity_token" in text:
            assert "SCRUBBED" in text, f"{path.name}: token left unscrubbed"

        leaked_emails = {
            m for m in real_email.findall(text) if not m.endswith("example.com")
        }
        assert not leaked_emails, f"{path.name}: unscrubbed email(s) {leaked_emails}"

        leaked_phones = {m for m in real_phone.findall(text) if m != "555-555-5555"}
        assert not leaked_phones, f"{path.name}: unscrubbed phone(s) {leaked_phones}"


# --------------------------------------------------------------------------
# widget internals
# --------------------------------------------------------------------------


def test_recaptcha_and_phone_widget_inputs_are_not_questions() -> None:
    """These parked a real Stripe application on unanswerable questions.

    reCAPTCHA's response textarea took "Submit application" as its label and
    was reported as a required field; the phone widget's country search box
    became "iti search input". Neither is part of the application.
    """
    html = """
      <form>
        <label for="first">First Name</label>
        <input id="first" name="first" type="text">
        <textarea id="g-recaptcha-response-100000" name="g-recaptcha-response"></textarea>
        <input id="iti-0__search-input" type="text">
        <input id="select2-country-search" type="text">
      </form>
    """
    labels = [f.label for f in extract_fields(html)]
    assert labels == ["First Name"]


def test_a_real_field_named_search_is_kept() -> None:
    """The guard is for widget internals, not for anything mentioning search."""
    html = """
      <form>
        <label for="q">What are you searching for in your next role?</label>
        <textarea id="q" name="search_reason"></textarea>
      </form>
    """
    assert len(extract_fields(html)) == 1


# --------------------------------------------------------------------------
# Ashby checkbox groups
# --------------------------------------------------------------------------


def _ashby():
    path = Path(__file__).parent / "fixtures" / "pages" / "ashby_checkbox_group.html"
    return extract_fields(path.read_text())


def test_ashby_options_collapse_into_one_question() -> None:
    """Ashby names each tick-box after its own option text.

    Grouping on `name` made every option a separate field, so the agent asked
    "Plaid's Mission" and "New York City Office" as if they were questions.
    """
    boxes = [f for f in _ashby() if f.type == "checkbox"]
    assert len(boxes) == 2


def test_ashby_group_is_named_by_its_question_not_its_first_option() -> None:
    boxes = [f for f in _ashby() if f.type == "checkbox"]
    labels = {f.label for f in boxes}
    assert "Why are you interested in working at Plaid? Select all that apply." in labels
    assert "Preferred Work Location" in labels
    assert "Plaid's Mission" not in labels
    assert "San Francisco HQ" not in labels


def test_ashby_options_are_kept_as_choices() -> None:
    """The option text is still needed — it is what gets ticked."""
    boxes = {f.label: f.options for f in _ashby() if f.type == "checkbox"}
    assert boxes["Preferred Work Location"] == [
        "San Francisco HQ", "New York City Office",
    ]
    assert "Plaid's Mission" in boxes[
        "Why are you interested in working at Plaid? Select all that apply."
    ]


def test_ashby_ordinary_fields_are_untouched() -> None:
    text = [f for f in _ashby() if f.type == "text"]
    assert [f.label for f in text] == ["Name"]


def test_two_groups_stay_separate() -> None:
    """Both fieldsets group by container; neither absorbs the other."""
    boxes = [f for f in _ashby() if f.type == "checkbox"]
    assert boxes[0].group != boxes[1].group


# --------------------------------------------------------------------------
# Lever question cards
# --------------------------------------------------------------------------


def _lever_card():
    path = Path(__file__).parent / "fixtures" / "pages" / "lever_question_card.html"
    return extract_fields(path.read_text())


def test_lever_card_is_named_by_its_question_not_its_input_name() -> None:
    """Lever uses no <fieldset>, so every strategy fell through to humanizing
    the input name — presenting "cards 18631c8a d2a4 41d9 ba8a…" as a required
    question the user was asked to answer."""
    labels = [f.label for f in _lever_card()]
    assert "Are you currently enrolled in a 2-year or 4-year college program?" in labels
    assert not any("cards" in label and "18631c8a" in label for label in labels)


def test_lever_card_options_collapse_into_one_question() -> None:
    boxes = [f for f in _lever_card() if f.type == "checkbox"]
    assert len(boxes) == 1
    assert boxes[0].options == ["Yes", "No"]


def test_lever_free_text_follow_up_takes_the_cards_question() -> None:
    """This one was labelled "No" — the option text sitting just before it."""
    texts = {f.label for f in _lever_card() if f.type == "text"}
    assert "Do you currently receive any active funding?" in texts
    assert "No" not in texts
    assert "Type your response" not in texts


def test_lever_ordinary_fields_are_untouched() -> None:
    named = [f for f in _lever_card() if f.name == "name"]
    assert [f.label for f in named] == ["Full name"]
    assert named[0].required


def test_a_typeahead_with_only_a_placeholder_gets_its_real_label() -> None:
    """Ashby's school and location pickers carry no id, name or aria-label —
    the placeholder was the only text found and became the label. No rule
    could match "Search schools...", and the question filter (rightly)
    refused to ask it, so the field could neither fill nor ask: a dead zone.
    Rejecting prompt-placeholders lets the label fall through to the text
    that actually names the field.
    """
    html = """
      <form>
        <div class="_fieldEntry">
          <label class="ashby-application-form-question-title">School</label>
          <input type="text" role="combobox" placeholder="Search schools...">
        </div>
      </form>
    """
    (field,) = extract_fields(html)
    assert field.label == "School"
    assert "search" not in field.label.lower()
