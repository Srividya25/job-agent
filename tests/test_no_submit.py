"""The scheduled batch must never submit.

Sri Vidya chose "fill and leave it for me" over auto-submission. That promise
is worth a test rather than a comment: submitting is the one act here that
cannot be undone.
"""

from __future__ import annotations

import asyncio

import pytest

from job_agent import run, schedule


def test_submit_refuses_unless_mode_is_auto() -> None:
    """The second lock: _submit itself checks, not just its caller."""
    with pytest.raises(RuntimeError, match="refusing to submit"):
        asyncio.run(
            run._submit(
                job=None, outcome=None, page=None, tracker=None, telegram=None,
                report=None, resume=None, submit_mode="never",
            )
        )


@pytest.mark.parametrize("mode", ["never", "ask", ""])
def test_submit_refuses_every_non_auto_mode(mode: str) -> None:
    with pytest.raises(RuntimeError):
        asyncio.run(
            run._submit(
                job=None, outcome=None, page=None, tracker=None, telegram=None,
                report=None, resume=None, submit_mode=mode,
            )
        )


def test_fill_batch_always_passes_never(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if the profile says auto, the batch path fills only.

    fill_batch hardcodes submit_mode rather than reading automation.submit_mode
    precisely so that editing the profile later cannot turn the 10am run into
    an auto-submitter.
    """
    seen: dict[str, object] = {}

    async def fake_apply_batch(profile, **kwargs):
        seen.update(kwargs)
        return run.RunReport()

    monkeypatch.setattr(run, "apply_batch", fake_apply_batch)
    monkeypatch.setattr(schedule.db, "mark_acted", lambda *a, **k: None)

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(schedule.db, "connect", lambda: FakeConn())

    asyncio.run(
        schedule.fill_batch(
            profile=object(), pairs=[(1, "some-key")], telegram=None
        )
    )
    assert seen["submit_mode"] == "never"


def test_fill_batch_with_nothing_chosen_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def fake_apply_batch(profile, **kwargs):
        nonlocal called
        called = True
        return run.RunReport()

    monkeypatch.setattr(run, "apply_batch", fake_apply_batch)
    filled = asyncio.run(
        schedule.fill_batch(profile=object(), pairs=[], telegram=None)
    )
    assert filled == 0
    assert not called


def test_manual_mode_fills_nothing() -> None:
    """Manual is the user doing it herself; the agent opens no forms."""
    assert schedule.to_fill(run_id=1, mode=schedule.Mode.MANUAL) == []


# --------------------------------------------------------------------------
# completeness
# --------------------------------------------------------------------------


def _outcome(resolved: int, unresolved: int):
    """An ApplyOutcome with a given fill ratio and nothing marked required."""
    from job_agent.apply import ApplyOutcome
    from job_agent.forms.extract import FormField
    from job_agent.resolve.engine import Answer, Resolution, Tier

    answers = [
        Answer(ref=f"#a{i}", label=f"A{i}", value="x", tier=Tier.RULES,
               field_type="text")
        for i in range(resolved)
    ]
    blanks = [
        FormField(ref=f"#u{i}", label=f"U{i}", type="text", required=False)
        for i in range(unresolved)
    ]
    out = ApplyOutcome(url="https://example.com")
    out.fields = [FormField(ref=a.ref, label=a.label, type="text") for a in answers]
    out.fields += blanks
    out.resolution = Resolution(answers=answers, unresolved=blanks)
    return out


def test_optional_unanswered_questions_are_not_asked() -> None:
    """Her rule: no red asterisk, no interruption — optional blanks are
    hers at review, required ones are the only questions worth sending."""
    from job_agent.apply import ApplyOutcome, unanswered_questions
    from job_agent.forms.extract import FormField
    from job_agent.resolve.engine import Resolution

    req = FormField(ref="#a", label="Work authorization?", type="radio",
                    required=True)
    opt = FormField(ref="#b", label="Additional Information", type="textarea",
                    required=False)
    outcome = ApplyOutcome(url="u")
    outcome.resolution = Resolution(answers=[], unresolved=[opt, req])
    assert [f.ref for f in unanswered_questions(outcome)] == ["#a"]


def test_a_mostly_empty_form_is_not_offered_for_approval(profile_fixture=None) -> None:
    """Plaid resolved 9 of 35 with nothing marked required, and was offered
    for submission anyway. Completeness has to be checked separately."""
    from job_agent.config import load_profile
    from job_agent.models import ATS, Job, JobStatus

    profile = load_profile()
    job = Job(dedupe_key="k", company="Plaid", title="SWE",
              url="https://example.com", ats=ATS.ASHBY, match_score=0.72,
              status=JobStatus.NEW)

    blocked = run.gate_blocks(_outcome(9, 26), job, profile, "never")
    assert "unanswered" in blocked


def test_a_complete_form_is_offered() -> None:
    from job_agent.config import load_profile
    from job_agent.models import ATS, Job, JobStatus

    profile = load_profile()
    job = Job(dedupe_key="k", company="Plaid", title="SWE",
              url="https://example.com", ats=ATS.ASHBY, match_score=0.72,
              status=JobStatus.NEW)

    assert run.gate_blocks(_outcome(33, 2), job, profile, "never") == ""


# --------------------------------------------------------------------------
# the server's verdict
# --------------------------------------------------------------------------


def test_a_rejection_body_is_recognised() -> None:
    """The exact shape Ashby returned while two false submissions were being
    recorded on page cosmetics."""
    body = {"data": {"submitApplicationFormAction": {
        "__typename": "SingleFormSubmitResult", "messages": None,
        "applicationFormResult": {"__typename": "FormRender", "id": "x",
            "errorMessages": ["Missing entry for required field: Phone Number"]}}}}
    kind, errors = run.classify_submit_body(body)
    assert kind == "rejected"
    assert "Phone Number" in errors[0]


def test_an_acceptance_body_is_recognised() -> None:
    body = {"data": {"submitApplicationFormAction": {
        "__typename": "SingleFormSubmitResult",
        "applicationFormResult": {"errorMessages": []}}}}
    assert run.classify_submit_body(body) == ("accepted", [])


def test_an_unrelated_body_is_no_verdict() -> None:
    assert run.classify_submit_body({"data": {"ok": True}}) == ("unknown", [])
