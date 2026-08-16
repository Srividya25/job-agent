"""The apply loop.

Runs unattended. For each queued job it fills the form, verifies the writes,
and then either submits or parks. It never waits: a job it cannot finish is
handed to Telegram as numbered questions and the loop moves straight on.

Answers arrive on the next run. That is what makes the system converge —
every question answered once becomes a Tier 1 cache hit forever, so the set
of things that can block shrinks with use.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from . import approve, propose
from .apply import ApplyOutcome, apply_to_page, unanswered_questions
from .browser import session as bs
from .config import Profile
from .fill.writer import best_option  # noqa: F401  (kept for symmetry of imports)
from .models import Job, JobStatus, is_workday
from .notify.telegram import (
    Telegram,
    format_applied,
    format_question,
    format_summary,
    question_buttons,
)
from .resolve.engine import scope_for
from .resolve import cache as answer_cache
from .store import db
from .tracker import Tracker, TrackerError, build_note

# The only two values of submit_mode that may reach the submit button.
SUBMIT_MODES = frozenset({"auto", "approved"})

# Above this share of unanswered fields an application is not worth offering
# for approval — it is worth asking about instead.
MAX_UNANSWERED_SHARE = 0.30

SUBMIT_SELECTORS = [
    "button[type=submit]",
    "input[type=submit]",
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "[data-automation-id='submitButton']",
]


@dataclass
class RunReport:
    applied: int = 0
    stuck: int = 0
    skipped: int = 0
    pending_approval: int = 0
    errors: list[str] = field(default_factory=list)
    answers_applied: int = 0

    def summary(self) -> str:
        return format_summary(self.applied, self.stuck, self.skipped)


# ----------------------------------------------------------------------
# inbound: consume replies before applying
# ----------------------------------------------------------------------


def consume_replies(telegram: Telegram | None) -> int:
    """Apply Telegram answers to the cache and re-queue the parked jobs.

    Runs at the start of every run, before any form is opened, so a job that
    was blocked yesterday is retried today with the answer already cached.
    """
    if telegram is None:
        return 0

    offset = _load_offset()
    replies, next_offset = telegram.poll(offset)
    if not replies:
        return 0
    applied = apply_replies(replies, telegram)
    _save_offset(next_offset)
    return applied


def apply_replies(replies, telegram) -> int:
    """Apply already-fetched replies. Shared with the standing listener."""
    applied = 0
    unmatched: list[str] = []

    with db.connect() as conn:
        # With exactly one question outstanding, a bare answer is unambiguous.
        # Requiring the number there is pedantry that loses real answers: the
        # obvious reply to a single question is just the answer.
        open_now = db.open_questions(conn)
        lone = open_now[0]["ordinal"] if len(open_now) == 1 else None

        for reply in replies:
            # "window 3d" is a standing setting she may type at any quiet
            # moment; this poller may be the first to see it, so it applies
            # it rather than merely not-eating it.
            if (window := propose.parse_since_command(reply.raw)) is not None:
                from . import schedule

                schedule.apply_since_command(telegram, window)
                continue

            # "6 ignore" / "auto" belong to a proposal batch and "submit 3"
            # to an approval — none of them are form answers. Without this,
            # "6 ignore" would be stored as the answer text of whichever open
            # question was numbered 6, and "submit 3" would be attached to a
            # lone open question by the bare-answer rule below. Both would
            # then be cached and replayed onto every future application.
            if propose.is_decision_reply(reply.raw) or approve.is_approval_reply(
                reply.raw
            ):
                continue

            ordinal = reply.ordinal if reply.ordinal else lone
            row = db.resolve_pending(conn, ordinal, reply.answer) if ordinal else None
            if row is None:
                unmatched.append(f"{reply.ordinal}. {reply.answer[:60]}")
                continue
            answer_cache.remember(
                row["question"], reply.answer, row["scope"] or "*",
                row["field_type"] or "text",
            )
            # Un-park the job so this run retries it.
            db.set_status(conn, row["dedupe_key"], JobStatus.NEW)
            applied += 1
            if not reply.ordinal and telegram is not None:
                # Attached by position rather than by number, so say what it
                # was attached to — a wrong guess must be obvious, not silent.
                telegram.send(
                    f"Recorded for \"{row['question'][:60]}\":\n{reply.answer[:80]}"
                )

    # Advancing the offset tells Telegram to discard these updates, so an
    # answer that matched nothing is gone for good. Say so rather than losing
    # it silently — this happened once with replies to a message that was
    # never persisted as questions, and the answers simply disappeared.
    if unmatched and telegram is not None:
        telegram.send(
            "⚠️ Could not match "
            f"{len(unmatched)} answer(s) to an open question:\n"
            + "\n".join(unmatched)
            + "\n\nThey were not saved. Reply again after I ask, "
            "or check `job-agent status`."
        )

    return applied


def _offset_path():
    from .config import data_dir

    return data_dir() / "telegram_offset"


def _load_offset() -> int:
    path = _offset_path()
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _save_offset(offset: int) -> None:
    try:
        _offset_path().write_text(str(offset))
    except OSError:
        pass


# ----------------------------------------------------------------------
# submitting
# ----------------------------------------------------------------------


async def click_submit(page) -> tuple[bool, str]:
    """Click the final submit control. Only ever called behind the gate."""
    for selector in SUBMIT_SELECTORS:
        button = page.locator(selector).first
        try:
            if await button.count() == 0 or not await button.is_visible():
                continue
            await button.click()
            await page.wait_for_load_state("networkidle", timeout=20000)
            return True, selector
        except Exception:  # noqa: BLE001 - try the next selector
            continue
    return False, "no submit button found"


def gate_blocks(
    outcome: ApplyOutcome, job: Job, profile: Profile, submit_mode: str = "auto"
) -> str:
    """Why this application may not be auto-submitted. Empty string = clear.

    The conditions are ADR-004's. Reported as a reason rather than a bool so
    a refusal can be explained rather than merely observed.

    Most of them are about *submitting*, so they do not apply to a run that
    only fills. Applying them there rejected jobs the user had just chosen by
    number — a 79% job was filled correctly and then filed as "skipped: below
    threshold", so she never saw it. When nothing will be submitted, the only
    real blockers are a banned portal and a form that cannot be completed.
    """
    if job.ats.value in profile.automation.never_apply_portals:
        return f"{job.ats.value} is on never_apply_portals"
    if "workday" in profile.automation.never_apply_portals and is_workday(
        job.ats, job.url
    ):
        # By URL as well as by enum: aggregator sources deliver Workday jobs
        # with ats UNKNOWN, and those must not slip past the ban.
        return "workday is on never_apply_portals"
    if outcome.resolution is None:
        # Prefer what actually went wrong ("sign-in required") over the
        # generic phrasing — this reason is what she reads on Telegram.
        return outcome.error or "nothing resolved"
    if outcome.resolution.blocking:
        return f"{len(outcome.resolution.blocking)} required field(s) unanswered"
    if outcome.fill is not None and not outcome.fill.ok:
        # Checked before the never-branch: six writes were failing on every
        # fill ("element not found") while the review presented their intended
        # values as filled. A write that did not land blocks in every mode.
        return f"{len(outcome.fill.failed)} field(s) failed to write"
    if submit_mode == "never":
        # She reviews a screenshot and every value before anything is sent;
        # that is a stronger check than the score threshold this would apply.
        #
        # Completeness is not covered by `blocking` above: that counts only
        # fields the DOM marks required, and Ashby marks almost nothing —
        # Plaid's form resolved 9 of 35 and still passed, so an application
        # missing its office preference, education dates and screening
        # answers was offered for submission as if it were ready.
        total = len(outcome.fields)
        unresolved = len(outcome.resolution.unresolved)
        if total and unresolved / total > MAX_UNANSWERED_SHARE:
            return f"{unresolved} of {total} fields unanswered"
        return ""
    if job.match_score < profile.preferences.min_match_score:
        return f"match {job.match_score:.0%} below threshold"
    if not outcome.resolution.all_confident:
        return "some answers are not from cache or rules"
    if outcome.verification is not None and not outcome.verification.ok:
        return f"{len(outcome.verification.mismatches)} field(s) did not verify"
    return ""


# ----------------------------------------------------------------------
# the loop
# ----------------------------------------------------------------------


async def apply_batch(
    profile: Profile,
    limit: int = 5,
    submit_mode: str = "never",
    min_score: float | None = None,
    max_age_hours: int = 0,
    only: list[str] | None = None,
    on_event=None,
    on_filled=None,
    keep_open: bool = False,
) -> RunReport:
    report = RunReport()
    telegram = Telegram.from_env()
    threshold = (
        min_score if min_score is not None else profile.preferences.min_match_score
    )

    # A profile that contradicts the resume must never reach an employer.
    # This is the guard that would have stopped "Santa Clara University" —
    # a fabricated school — going out on a real application.
    if submit_mode in {"ask", "auto"}:
        from .match.resume import load_resumes
        from .profile_check import Level, check_all

        contradictions = [
            f"{label}: {f.field} — {f.detail}"
            for label, findings in check_all(
                profile, load_resumes(profile.resumes)
            ).items()
            for f in findings
            if f.level is Level.ERROR
        ]
        if contradictions:
            report.errors.append(
                "profile contradicts the resume; refusing to submit:\n  "
                + "\n  ".join(contradictions[:5])
            )
            if telegram:
                telegram.send(
                    "🛑 Not applying: profile.yaml contradicts your resume.\n"
                    + "\n".join(contradictions[:5])
                    + "\n\nRun: job-agent check"
                )
            return report

    report.answers_applied = consume_replies(telegram)

    with db.connect() as conn:
        if only is not None:
            # An explicit list is a decision the user already made, job by
            # job. Re-filtering it by score or age here would silently drop
            # something she asked for by number.
            jobs = [j for key in only if (j := db.job_by_key(conn, key))]
        else:
            jobs = db.list_jobs(
                conn, status=JobStatus.NEW, min_score=threshold,
                limit=limit, max_age_hours=max_age_hours,
            )
    if not jobs:
        return report

    tracker: Tracker | None = None
    if submit_mode == "auto":
        try:
            tracker = Tracker.connect()
        except TrackerError as exc:
            report.errors.append(f"tracker unavailable: {exc}")

    session = await bs.attach()
    try:
        await session.sweep_leftover_tabs(keep=1)

        for job in jobs:
            resume = _resume_for(profile, job)
            try:
                page = await session.open(job.url)
                outcome = await apply_to_page(
                    page, profile, resume.path if resume else None, job=job
                )
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the run
                report.errors.append(f"{job.company}: {type(exc).__name__}")
                report.skipped += 1
                continue

            blocked = gate_blocks(outcome, job, profile, submit_mode)
            if on_event:
                on_event(job, outcome, blocked)

            # Park whenever something is blocked and there is anything to ask
            # about — not only when the DOM marked a field required. An
            # incomplete application is a question, not a skip: answered once,
            # each field becomes a cache hit for every later form.
            if blocked and outcome.resolution and outcome.resolution.unresolved:
                await _park(job, outcome, telegram, page, profile)
                report.stuck += 1
                if telegram:
                    await _hold_for_hand_fill(
                        job, outcome, page, telegram, minutes=5, on_event=None
                    )
            elif blocked:
                with db.connect() as conn:
                    db.set_status(conn, job.dedupe_key, JobStatus.NEEDS_REVIEW)
                report.skipped += 1
                if telegram:
                    # Failed writes are not questions she can answer by text —
                    # the window and her hands are the recovery path.
                    await _hold_for_hand_fill(
                        job, outcome, page, telegram, minutes=5, on_event=None
                    )
            elif submit_mode == "auto":
                await _submit(
                    job, outcome, page, tracker, telegram, report, resume,
                    submit_mode=submit_mode,
                )
            elif submit_mode == "ask":
                with db.connect() as conn:
                    db.set_status(conn, job.dedupe_key, JobStatus.PENDING_APPROVAL)
                report.pending_approval += 1
                if telegram:
                    telegram.send(
                        f"🟢 Ready to submit: {job.title} — {job.company}\n"
                        f"match {job.match_score:.0%} · all fields verified\n{job.url}"
                    )
            else:  # never
                report.skipped += 1
                # The tab is reused for the next job and closed at the end of
                # the run, so anything the caller wants from this filled form
                # — a screenshot, the values — has to be taken now.
                if on_filled is not None:
                    await on_filled(job, outcome, page)

            await asyncio.sleep(profile.automation.delay_between_apps)
    finally:
        # keep_open leaves the filled tab on screen so she can finish the
        # fields nothing could answer. Closing it discarded the fill, which
        # made "fill and leave it for me" impossible to actually do.
        if not keep_open:
            await session.close()
        else:
            await session.detach()

    if telegram:
        telegram.send(report.summary())
    return report


def _resume_for(profile: Profile, job: Job):
    for ref in profile.resumes:
        if ref.label == job.best_resume:
            return ref
    return profile.resumes[0] if profile.resumes else None


_YES_NO = re.compile(
    r"^\s*(are|do|does|did|have|has|will|would|can|is|were|any)\b|\?\s*$", re.I
)
_LOCATION_Q = re.compile(
    r"\b(city|town|location|where\s+(do|are)\s+you|reside|based)\b", re.I
)
_COUNTRY_Q = re.compile(r"\bcountry\b", re.I)


def suggestions(field, profile: Profile, job: Job) -> list[str]:
    """Answers to offer as buttons when the form itself lists none.

    Typeahead controls — the city picker, the school picker — query a server
    per keystroke, so nothing is in the DOM to extract and a question about
    location arrived with no way to tap an answer. These are the candidates a
    person would pick from anyway: where she lives, where the job is, Remote.
    """
    label = field.question or field.label or ""

    # A lone tick-box's only "option" is its own label, so offering it as a
    # button gives one choice meaning "tick this" — and recording the label as
    # the answer ticked "Still Student?" on a form where she is not a student.
    # A single yes/no control deserves a yes/no answer.
    lone_checkbox = field.type == "checkbox" and len(field.options) <= 1
    if field.options and not lone_checkbox:
        return list(field.options)
    if lone_checkbox:
        return ["Yes", "No"]

    if _COUNTRY_Q.search(label):
        return [profile.address.country]

    if _LOCATION_Q.search(label):
        city = profile.address.city
        options = [city, f"{city}, {profile.address.state}"]
        if job.location and job.location not in options:
            options.append(job.location[:40])
        options.append("Remote")
        return list(dict.fromkeys(o for o in options if o))

    if field.type in {"checkbox", "radio"} or _YES_NO.search(label):
        return ["Yes", "No"]

    return []


async def _hold_for_hand_fill(
    job, outcome, page, telegram, minutes: int = 5, on_event=None
) -> bool:
    """Her procedure for a stuck application, verbatim:

    "next time if you get stuck put the timer for 5 min and I'll fill the
    fields you got stuck and press done in the telegram — at that time
    capture my answers and try to fill them next time."

    The window stays open, Telegram lists exactly what could not be filled,
    and on `done` (or timeout) whatever she typed is read back and learned,
    so the next form asking the same thing fills itself.
    """
    import time as _time

    from . import schedule
    from .apply import form_root
    from .fill.verify import read_answer
    from .resolve.rules import _control_unit

    stuck = list(outcome.resolution.unresolved) if outcome.resolution else []
    failed = list(outcome.fill.failed) if outcome.fill else []
    lines = [f"  • {(f.question or f.label)[:58]}" for f in stuck]
    lines += [f"  • {w.label[:44]} — write failed: {w.detail[:40]}" for w in failed]
    telegram.send(
        f"🖊 Stuck on {job.title} — {job.company}.\n"
        f"The browser window is OPEN with everything else filled.\n\n"
        f"I could not finish:\n" + "\n".join(lines[:12]) +
        f"\n\nFill these by hand within {minutes} min, then reply `done`. "
        "I'll capture your answers and use them next time."
    )

    deadline = _time.time() + minutes * 60
    offset = schedule._load_offset()
    done = False
    while _time.time() < deadline and not done:
        texts, offset, callbacks = schedule._fetch_messages(telegram, offset)
        schedule._save_offset(offset)
        for data, query_id in callbacks:
            schedule.handle_common_callback(telegram, data, query_id)
        done = any(t.strip().lower() in {"done", "finished", "ok done"}
                   for t in texts) or schedule.hand_done_signalled()
        if not done:
            await asyncio.sleep(10)

    root, fields = await form_root(page)
    known = {
        a.label: a.value
        for a in (outcome.resolution.answers if outcome.resolution else [])
    }
    scope = scope_for(page.url)
    learned = 0
    for field_ in fields:
        if not field_.label or field_.type == "file" or _control_unit(field_):
            continue
        value = await read_answer(root, field_)
        if value and known.get(field_.label) != value:
            answer_cache.remember(field_.label, value, scope, field_.type)
            learned += 1
            if on_event:
                on_event(f"learned: {field_.label[:40]} = {value[:30]}")
            # A hand-fill answers the parked question too; otherwise the
            # refill would sit waiting for a Telegram tap that never comes.
            with db.connect() as conn:
                conn.execute(
                    "update pending_questions set answer = ?, answered_at = "
                    "datetime('now') where dedupe_key = ? and question = ? "
                    "and answered_at is null",
                    (value, job.dedupe_key, field_.label),
                )
    telegram.send(
        (f"✅ Got it — learned {learned} answer(s) from your fills."
         if done else
         f"⏳ Time's up — learned {learned} answer(s) from what was filled.")
    )
    return done


async def _park(
    job: Job, outcome: ApplyOutcome, telegram, page, profile: Profile | None = None
) -> None:
    """Save the open questions, message them, and move on."""
    questions = unanswered_questions(outcome)[:8]
    scope = scope_for(job.url)

    ids: list[int] = []
    with db.connect() as conn:
        db.clear_pending(conn, job.dedupe_key)
        for ordinal, field_ in enumerate(questions, start=1):
            ids.append(db.add_pending(
                conn, job.dedupe_key, ordinal, field_.label,
                field_.ref, field_.type, scope,
                full_question=field_.question or field_.label,
                options=(
                    suggestions(field_, profile, job) if profile else field_.options
                ),
            ))
        db.set_status(conn, job.dedupe_key, JobStatus.NEEDS_REVIEW)

    if telegram:
        telegram.send(
            f"🟡 Stuck on {job.title} — {job.company}\n"
            f"{len(questions)} question(s) below.\n{job.url}"
        )
        try:
            telegram.send_photo(await page.screenshot(full_page=False))
        except Exception:  # noqa: BLE001 - a screenshot is a nicety
            pass
        # One message per question, so the choices can be tapped rather than
        # transcribed. A list of bare labels in a single message was the thing
        # she could not answer.
        for ordinal, (field_, pending_id) in enumerate(
            zip(questions, ids, strict=True), start=1
        ):
            choices = (
                suggestions(field_, profile, job) if profile else field_.options
            )
            telegram.send(
                format_question(
                    ordinal, field_.question or field_.label, field_.type,
                    choices, job.company, job.title,
                ),
                buttons=question_buttons(pending_id, choices),
            )


def classify_submit_body(payload) -> tuple[str, list[str]]:
    """What a submit endpoint's JSON response says: accepted, rejected, or n/a.

    Ashby answers ApiSubmitSingleApplicationFormAction with errorMessages when
    it refuses — "Missing entry for required field: Phone Number" — while the
    page looks close enough to success that two false submissions were
    recorded before anyone read this. The server's verdict outranks anything
    the page shows.
    """
    errors: list[str] = []
    saw_submit_shape = False

    def walk(node) -> None:
        nonlocal saw_submit_shape
        if isinstance(node, dict):
            if "errorMessages" in node:
                saw_submit_shape = True
                errors.extend(str(e) for e in (node.get("errorMessages") or []))
            if str(node.get("__typename", "")).lower().endswith("submitresult"):
                saw_submit_shape = True
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    if errors:
        return "rejected", errors
    if saw_submit_shape:
        return "accepted", []
    return "unknown", []


_SUBMIT_ENDPOINT = re.compile(r"submit", re.I)


_SUCCESS_MARKERS = (
    "thank you for applying", "application has been submitted",
    "successfully submitted", "application received", "we've received your",
    "thanks for applying", "your application was submitted",
)
_FAILURE_MARKERS = (
    "is required", "field is required", "please complete", "captcha",
    "verify you are human", "something went wrong", "try again",
)


async def _confirm_submission(page, telegram, job) -> tuple[bool, str]:
    """Read the page's own verdict after the submit click.

    Returns (accepted, detail) and sends her the post-click screenshot either
    way — the screenshot is the receipt, whatever the markers say.
    """
    try:
        # Two readings four seconds apart: React re-renders the form after a
        # rejected submit, and a single reading caught the transient empty
        # state and reported success on an application the server had just
        # refused. Only a form that is gone and STAYS gone counts.
        await page.wait_for_timeout(4000)
        controls_first = await page.evaluate(
            "() => document.querySelectorAll('input,select,textarea').length"
        )
        await page.wait_for_timeout(4000)
        text = (await page.evaluate("() => document.body.innerText")).lower()
        controls = max(controls_first, await page.evaluate(
            "() => document.querySelectorAll('input,select,textarea').length"
        ))
    except Exception as exc:  # noqa: BLE001 - an unreadable page is a failure
        return False, f"could not read post-submit page: {type(exc).__name__}"

    if telegram:
        try:
            telegram.send_photo(
                await page.screenshot(full_page=False),
                f"After clicking submit: {job.title[:60]} — {job.company}",
            )
        except Exception:  # noqa: BLE001 - the verdict below still stands
            pass

    for marker in _FAILURE_MARKERS:
        if marker in text:
            return False, f"page reports a problem: {marker!r}"
    for marker in _SUCCESS_MARKERS:
        if marker in text:
            return True, f"page confirms: {marker!r}"
    if controls <= 2:
        # The form is gone. Not as strong as an explicit thank-you, but a
        # page that swallowed its own form has accepted something.
        return True, "form is gone from the page"
    return False, f"no success marker and {controls} form controls still present"


async def _submit(
    job, outcome, page, tracker, telegram, report, resume, submit_mode: str
) -> None:
    """Click submit. The only place in the codebase that does.

    Only two modes may submit: "auto" (unattended, currently unused) and
    "approved" (she replied `submit <n>` to a specific filled form). The
    caller already checks, so this re-check is redundant — deliberately.
    Submitting is the one irreversible act here, and a future refactor that
    adds a third call site should fail loudly rather than quietly send an
    application. Cheap lock on an expensive door.
    """
    if submit_mode not in SUBMIT_MODES:
        raise RuntimeError(
            f"_submit reached with submit_mode={submit_mode!r}; refusing to submit"
        )

    # The server's verdict, when the ATS exposes one, is the only evidence
    # that survived contact with reality: two page-confirmed "submissions"
    # were rejections the page rendered politely.
    verdicts: list[tuple[str, list[str]]] = []

    async def _read_verdict(response) -> None:
        if response.request.method != "POST":
            return
        if not _SUBMIT_ENDPOINT.search(response.url):
            return
        try:
            import json as _json

            verdicts.append(classify_submit_body(_json.loads(await response.text())))
        except Exception:  # noqa: BLE001 - an unreadable body is no verdict
            pass

    page.on("response", lambda r: asyncio.ensure_future(_read_verdict(r)))

    ok, detail = await click_submit(page)
    await page.wait_for_timeout(3000)

    rejected = [e for kind, errs in verdicts if kind == "rejected" for e in errs]
    accepted = any(kind == "accepted" for kind, _ in verdicts)
    if rejected:
        ok, detail = False, "server rejected: " + "; ".join(rejected[:4])[:300]
    elif accepted:
        ok, detail = True, "server accepted the submission"
    elif ok:
        # No verdict endpoint fired (or it was unreadable) — fall back to
        # what the page shows, demanding it say so and stay that way.
        ok, detail = await _confirm_submission(page, telegram, job)
    if not ok:
        with db.connect() as conn:
            db.set_status(conn, job.dedupe_key, JobStatus.NEEDS_REVIEW)
        report.stuck += 1
        if telegram:
            telegram.send(
                f"🟡 Filled but could not confirm submission: "
                f"{job.title} — {job.company}\n{detail}"
            )
        return

    with db.connect() as conn:
        db.set_status(conn, job.dedupe_key, JobStatus.APPLIED)
    report.applied += 1

    if tracker is not None:
        counts = outcome.resolution.summary() if outcome.resolution else {}
        note = build_note(
            job.match_score, job.best_resume,
            counts.get("cache", 0), counts.get("rules", 0), 0,
        )
        try:
            tracker.record(
                company=job.company, job_title=job.title, job_url=job.url,
                job_description=job.description or "",
                resume_label=job.best_resume,
                tracker_file=resume.tracker_file if resume else "",
                note=note,
            )
        except TrackerError as exc:
            report.errors.append(f"tracker write failed: {exc}")

    if telegram:
        telegram.send(
            format_applied(job.company, job.title, job.match_score, job.best_resume or "—")
        )
