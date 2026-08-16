"""Approval: the drift guard, the reply syntax, and what must not be eaten.

The drift check is what makes an approval mean something. Approving is
approving a specific set of values; if the form fills differently the second
time, the approval no longer covers what would be sent.
"""

from __future__ import annotations

import pytest

from job_agent import approve, propose, run
from job_agent.notify.telegram import parse_replies


def values(*pairs: tuple[str, str, str]) -> list[dict[str, str]]:
    return [{"ref": r, "label": lbl, "value": v} for r, lbl, v in pairs]


BASE = values(
    ("#first", "First Name", "Jane"),
    ("#email", "Email", "jane.doe@example.com"),
    ("#sponsor", "Require sponsorship?", "Yes"),
)


# --------------------------------------------------------------------------
# drift
# --------------------------------------------------------------------------


def test_identical_forms_do_not_drift() -> None:
    assert approve.drift(BASE, BASE) == []


def test_changed_value_is_caught() -> None:
    changed = values(
        ("#first", "First Name", "Jane"),
        ("#email", "Email", "someone.else@example.com"),
        ("#sponsor", "Require sponsorship?", "Yes"),
    )
    (change,) = approve.drift(BASE, changed)
    assert change.label == "Email"
    assert change.approved == "jane.doe@example.com"
    assert change.current == "someone.else@example.com"


def test_reordered_fields_are_not_drift() -> None:
    """Compared by ref, so a form that reshuffles has not changed."""
    shuffled = [BASE[2], BASE[0], BASE[1]]
    assert approve.drift(BASE, shuffled) == []


def test_disappearing_field_is_caught() -> None:
    (change,) = approve.drift(BASE, BASE[:2])
    assert change.label == "Require sponsorship?"
    assert change.current == "<field gone>"


def test_new_filled_field_is_caught() -> None:
    """A field she never saw, now carrying a value, must stop the submit."""
    extra = BASE + values(("#salary", "Expected salary", "90000"))
    (change,) = approve.drift(BASE, extra)
    assert change.label == "Expected salary"
    assert change.approved == "<not shown to you>"


def test_new_blank_field_is_not_drift() -> None:
    extra = BASE + values(("#optional", "Optional note", "  "))
    assert approve.drift(BASE, extra) == []


def test_growth_only_drift_is_a_delta_not_a_dead_end() -> None:
    """New fields with every approved value intact re-ask, never abandon."""
    extra = BASE + values(("#consent", "I consent to processing", "Yes"))
    changes = approve.drift(BASE, extra)
    delta = approve.only_new_fields(changes)
    assert delta is not None
    assert [c.label for c in delta] == ["I consent to processing"]


def test_mixed_drift_stays_a_hard_stop() -> None:
    """A changed approved value alongside a new field must still block."""
    mixed = values(
        ("#first", "First Name", "Jane"),
        ("#email", "Email", "someone.else@example.com"),
        ("#sponsor", "Require sponsorship?", "Yes"),
        ("#consent", "I consent to processing", "Yes"),
    )
    assert approve.only_new_fields(approve.drift(BASE, mixed)) is None


def test_clean_refill_is_no_delta() -> None:
    assert approve.only_new_fields(approve.drift(BASE, BASE)) is None


def test_delta_message_shows_the_new_values_and_the_way_forward() -> None:
    changes = approve.drift(BASE, BASE + values(("#c", "Consent", "Yes")))
    body = approve.format_delta(3, "Stripe", "Backend Engineer", changes)
    assert "Consent: Yes" in body
    assert "submit 3" in body
    assert "nothing was sent" in body.lower()


def test_a_wrong_school_would_be_caught() -> None:
    """The failure this whole system exists to prevent."""
    approved = values(("#edu", "University", "Bayview State University"))
    current = values(("#edu", "University", "Santa Clara University"))
    (change,) = approve.drift(approved, current)
    assert "Santa Clara" in change.current


def test_roundtrip_through_storage() -> None:
    assert approve.loads(approve.dumps(BASE)) == BASE


def test_corrupt_snapshot_reads_as_empty() -> None:
    assert approve.loads("not json") == []
    assert approve.loads("") == []


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("submit 3", 3), ("SUBMIT #12", 12), ("  submit 7  ", 7),
     ("submit", None), ("submit all", None), ("3 submit", None)],
)
def test_parse_submit(text: str, expected: int | None) -> None:
    assert approve.parse_submit(text) == expected


def test_parse_skip() -> None:
    assert approve.parse_skip("skip 4") == 4
    assert approve.parse_skip("drop 4") == 4
    assert approve.parse_skip("skip") is None


def test_submit_all() -> None:
    assert approve.is_submit_all("submit all")
    assert not approve.is_submit_all("submit 3")


# --------------------------------------------------------------------------
# routing: three consumers share one Telegram inbox
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["submit 3", "skip 3", "submit all"])
def test_approval_replies_are_owned(text: str) -> None:
    assert approve.is_approval_reply(text)
    assert not propose.is_decision_reply(text)


@pytest.mark.parametrize("text", ["auto", "manual", "6 ignore"])
def test_proposal_replies_are_not_approvals(text: str) -> None:
    assert not approve.is_approval_reply(text)


@pytest.mark.parametrize("text", ["2 Yes", "120000", "Master of Science"])
def test_form_answers_belong_to_neither(text: str) -> None:
    assert not approve.is_approval_reply(text)
    assert not propose.is_decision_reply(text)


def test_submit_reply_would_otherwise_be_eaten_as_a_lone_answer() -> None:
    """Why the guard in consume_replies is necessary, not decorative.

    "submit 3" has no leading ordinal, so telegram.parse_replies hands it back
    with ordinal 0 — and consume_replies attaches an ordinal-0 reply to the
    open question when exactly one is outstanding. Without the guard, an
    approval would be filed as the answer to a form field.
    """
    (reply,) = parse_replies("submit 3")
    assert reply.ordinal == 0
    assert approve.is_approval_reply(reply.raw)


# --------------------------------------------------------------------------
# the submit lock
# --------------------------------------------------------------------------


def test_approved_is_a_permitted_submit_mode() -> None:
    assert "approved" in run.SUBMIT_MODES
    assert "auto" in run.SUBMIT_MODES
    assert "never" not in run.SUBMIT_MODES
    assert "ask" not in run.SUBMIT_MODES


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------


def test_review_message_shows_every_value_and_the_reply_syntax() -> None:
    body = approve.format_review(
        3, "Stripe", "Backend Engineer", 0.79, "data",
        "https://example.com/x", BASE,
    )
    assert "Jane" in body
    assert "jane.doe@example.com" in body
    assert "submit 3" in body and "skip 3" in body
    assert "79%" in body


def test_review_flags_blank_fields() -> None:
    body = approve.format_review(
        1, "Acme", "SWE", 0.7, "general", "https://x", BASE, unresolved=2
    )
    assert "2 left blank" in body
    assert "filled 3 of 5 fields" in body


def test_drift_message_names_what_changed() -> None:
    changes = approve.drift(BASE, values(("#email", "Email", "x@y.z")))
    body = approve.format_drift("Stripe", "Backend Engineer", changes)
    assert "Not submitting" in body
    assert "Nothing was sent" in body


# --------------------------------------------------------------------------
# login walls
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    ["Password", "Verify New Password", "Sign in", "Create an account"],
)
def test_login_wall_labels_are_recognised(label: str) -> None:
    """Workday's Apply lands on a signup form, not an application.

    Filling it would send back a screenshot that looks like a completed
    application and is really an account being created on the employer's site.
    """
    from job_agent.apply import _LOGIN_WALL

    assert _LOGIN_WALL.search(label)


@pytest.mark.parametrize("label", ["First Name", "Email", "Why this role?"])
def test_ordinary_labels_are_not_login_walls(label: str) -> None:
    from job_agent.apply import _LOGIN_WALL

    assert not _LOGIN_WALL.search(label)


# --------------------------------------------------------------------------
# multi-select and editing
# --------------------------------------------------------------------------


def test_select_all_that_apply_is_recognised() -> None:
    """A five-option question went out carrying only the last tap."""
    from job_agent.notify.telegram import is_multi_select

    assert is_multi_select("checkbox", ["a", "b", "c"])
    assert is_multi_select(
        "text", ["a", "b"], "Why are you interested? Select all that apply."
    )
    # One-of controls stay one-of.
    assert not is_multi_select("radio", ["Yes", "No"])
    assert not is_multi_select("select", ["Yes", "No"])
    assert not is_multi_select("checkbox", ["Only one"])


def test_multi_select_offers_a_done_button_and_ticks_choices() -> None:
    from job_agent.notify.telegram import question_buttons

    rows = question_buttons(7, ["Mission", "Fintech", "AI"], multi=True,
                            chosen=["Fintech"])
    flat = [label for row in rows for label, _ in row]
    assert any("☑️" in label and "Fintech" in label for label in flat)
    assert any("☐" in label and "Mission" in label for label in flat)
    assert flat[-1] == "✅ Done"


def test_done_callback_parses() -> None:
    from job_agent.notify.telegram import parse_question_callback

    assert parse_question_callback("q:7:done") == (7, "done")
    assert parse_question_callback("q:7:2") == (7, 2)


def test_review_offers_edit_alongside_submit() -> None:
    flat = [label for row in approve.review_buttons(3) for label, _ in row]
    assert any("Change an answer" in label for label in flat)
    assert any("Submit" in label for label in flat)
    assert approve.parse_review_callback("a:3:edit") == (3, "edit")


def test_edit_reply_parses_and_is_owned() -> None:
    assert approve.parse_edit("edit 3 San Jose") == (3, "San Jose")
    assert approve.parse_edit("edit 12 Master of Science") == (12, "Master of Science")
    assert approve.is_approval_reply("edit 3 San Jose")
    assert approve.parse_edit("edit") is None


def test_snapshot_carries_options_so_a_value_can_be_retapped() -> None:
    from job_agent.forms.extract import FormField
    from job_agent.resolve.engine import Answer, Resolution, Tier

    answers = [Answer(ref="#o", label="Office", value="SF", tier=Tier.RULES,
                      field_type="checkbox")]
    fields = [FormField(ref="#o", label="Office", type="checkbox",
                        options=["SF", "NYC"])]
    (item,) = approve.snapshot(Resolution(answers=answers, unresolved=[]), fields)
    assert item["options"] == ["SF", "NYC"]


def test_editing_a_multi_select_field_offers_multi_select() -> None:
    """A "select all that apply" field could only be corrected to one value."""
    rows = approve.value_buttons(
        1, 2, ["Mission", "Fintech", "AI"], multi=True, chosen=["AI"]
    )
    flat = [label for row in rows for label, _ in row]
    assert any("☑️" in label and "AI" in label for label in flat)
    assert flat[-1] == "✅ Done"
    assert approve.parse_value_callback("v:1:2:done") == (1, 2, "done")
    assert approve.parse_value_callback("v:1:2:0") == (1, 2, 0)


def test_editing_a_one_of_field_stays_one_of() -> None:
    rows = approve.value_buttons(1, 2, ["Yes", "No"])
    flat = [label for row in rows for label, _ in row]
    assert "✅ Done" not in flat
    assert not any("☐" in label for label in flat)


def test_snapshot_carries_the_real_control_type() -> None:
    """Inferring the type from option count called a 13-month date dropdown a
    multi-select, and offered Done on a one-of question."""
    from job_agent.forms.extract import FormField
    from job_agent.notify.telegram import is_multi_select
    from job_agent.resolve.engine import Answer, Resolution, Tier

    answers = [
        Answer(ref="#m", label="Start Date", value="May", tier=Tier.CACHE,
               field_type="select"),
        Answer(ref="#w", label="Why?", value="Mission", tier=Tier.CACHE,
               field_type="checkbox"),
    ]
    fields = [
        FormField(ref="#m", label="Start Date", type="select",
                  options=["January", "February", "May"]),
        FormField(ref="#w", label="Why?", type="checkbox",
                  options=["Mission", "Fintech"]),
    ]
    date_item, why_item = approve.snapshot(
        Resolution(answers=answers, unresolved=[]), fields
    )
    assert date_item["type"] == "select"
    assert not is_multi_select(date_item["type"], date_item["options"],
                               date_item["label"])
    assert is_multi_select(why_item["type"], why_item["options"], why_item["label"])


def test_ref_churn_is_not_drift() -> None:
    """Ashby regenerates element ids per visit. An identical refill under new
    refs reported every answer as gone + new — false drift blocking an
    approved submission."""
    approved = values(("#old-uuid-1", "Preferred Work Location", "San Francisco HQ"))
    current = values(("#new-uuid-9", "Preferred Work Location", "San Francisco HQ"))
    assert approve.drift(approved, current) == []


def test_split_date_halves_are_not_drift_whatever_their_refs() -> None:
    approved = values(("#m1", "Start Date", "August"), ("#y1", "Start Date", "2021"))
    current = values(("#y2", "Start Date", "2021"), ("#m2", "Start Date", "August"))
    assert approve.drift(approved, current) == []


def test_a_changed_value_still_trips_under_ref_churn() -> None:
    approved = values(("#a", "School", "Bayview State University"))
    current = values(("#b", "School", "Santa Clara University"))
    (change,) = approve.drift(approved, current)
    assert "Santa Clara" in change.current
