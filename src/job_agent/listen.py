"""The standing listener: Telegram answers within seconds, around the clock.

Before this, a message was only seen when some run happened to poll — a
`window 3d` typed at noon sat unanswered until the 15:00 batch, which reads
as the bot ignoring her. This process runs all day (its own LaunchAgent,
KeepAlive) and is the single consumer of the Telegram stream whenever no
batch is running.

Coordination rule: while a `job-agent batch` process is alive, the listener
does not poll at all — the batch owns the conversation (its collect/hold
loops are the ones showing messages the replies refer to). The moment the
batch exits, the listener resumes. It keeps its own offset high-water mark;
the batch-side offsets remain untouched.

What it handles, in routing order:
  taps      question choices, application outcomes, review buttons
            (submit / skip / edit / open-window), decision buttons —
            including taps on batches that are long over
  texts     `window 3d` · submit/skip/edit approvals · batch decisions
            (`auto`, `rest ignore`, `12 manual`) applied to the latest
            batch · everything else is treated as a form answer
  unknown   gets a short honest reply instead of silence
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from . import approve, propose, schedule
from .config import Profile, data_dir
from .notify.telegram import Telegram, parse_replies
from .store import db

POLL_SECONDS = 5  # she is waiting for an answer; be quick but polite


def _offset_path() -> Path:
    return data_dir() / "listen_offset"


def _load_offset() -> int:
    try:
        return int(_offset_path().read_text().strip())
    except (OSError, ValueError):
        return 0


def _save_offset(value: int) -> None:
    try:
        _offset_path().write_text(str(value))
    except OSError:
        pass


def batch_running() -> bool:
    out = subprocess.run(
        ["pgrep", "-f", "job-agent batch"], capture_output=True, text=True
    ).stdout.strip()
    return bool(out)


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------


def _fill(profile: Profile, telegram, pairs: list[tuple[int, str]]) -> None:
    if pairs:
        asyncio.run(schedule.fill_batch(profile, pairs, telegram))


def _route_callback(profile: Profile, telegram, data: str, query_id: str) -> None:
    if schedule.handle_common_callback(telegram, data, query_id):
        return

    # Decision buttons carry their run id, so taps keep working after the
    # batch process is gone — "That batch is over" punished her for
    # answering on her own schedule.
    if (parsed := propose.parse_callback(data)) is not None:
        kind, run_id, ordinal, value = parsed
        if kind == "d" and ordinal is not None:
            with db.connect() as conn:
                row = db.set_decision(conn, run_id, ordinal, value)
            telegram.answer_callback(
                query_id, f"{ordinal} → {value}" if row else f"No job {ordinal}"
            )
            if row is None:
                return
            with db.connect() as conn:
                job = db.job_by_key(conn, row["dedupe_key"])
            if job is None:
                return
            if value == propose.Decision.MANUAL.value:
                schedule.send_outcome_prompt(telegram, job, telegram.jobs_chat_id)
            elif value == propose.Decision.AUTO.value:
                with db.connect() as conn:
                    run = db.latest_proposal_run(conn)
                if run is not None and run["id"] == run_id and run["mode"] == "auto":
                    # The batch already got its go signal; a late auto pick
                    # fills now rather than waiting for tomorrow.
                    _fill(profile, telegram, [(row["id"], row["dedupe_key"])])
        elif kind == "m":
            with db.connect() as conn:
                db.set_proposal_mode(conn, run_id, value)
            telegram.answer_callback(query_id, f"Starting — {value}")
            if value == propose.Mode.AUTO.value:
                _fill(profile, telegram, schedule.to_fill(run_id, propose.Mode.AUTO))
        elif kind == "b":
            with db.connect() as conn:
                n = db.decide_remaining(conn, run_id, value)
            telegram.answer_callback(query_id, f"{n} set to {value}")
        return

    if (field_pick := approve.parse_field_callback(data)) is not None:
        schedule._offer_new_value(telegram, query_id, *field_pick)
        return
    if (new_value := approve.parse_value_callback(data)) is not None:
        schedule._apply_edit(profile, telegram, query_id, *new_value)
        return

    if (review := approve.parse_review_callback(data)) is not None:
        n, choice = review
        with db.connect() as conn:
            row = db.approval_by_ordinal(conn, n)
        if row is None:
            telegram.answer_callback(query_id, f"#{n} is already decided.")
            return
        if choice == "window":
            telegram.answer_callback(query_id, "Opening the window…")
            asyncio.run(schedule.window_session(
                profile, row["dedupe_key"], telegram, minutes=45))
            return
        if choice == "edit":
            values = approve.loads(row["values_json"])
            telegram.answer_callback(query_id, "Pick a field to change")
            telegram.send(
                f"✏️ Which answer should change on #{n}?",
                buttons=approve.field_buttons(n, values),
            )
            return
        from .models import JobStatus

        with db.connect() as conn:
            db.set_approval(conn, row["id"], choice)
            if choice == "skip":
                db.set_status(conn, row["dedupe_key"], JobStatus.SKIPPED)
        if choice == "skip":
            telegram.answer_callback(query_id, f"Dropped #{n}")
            return
        telegram.answer_callback(query_id, f"Submitting #{n}…")
        asyncio.run(approve.submit_approved(profile, row, telegram))
        return

    telegram.answer_callback(query_id, "I don't recognize that button.")


def _route_command_line(profile: Profile, telegram, line: str) -> bool:
    """One line that is a command rather than an answer. True if handled."""
    if (window := propose.parse_since_command(line)) is not None:
        schedule.apply_since_command(telegram, window)
        return True

    if (n := approve.parse_submit(line)) is not None or approve.is_submit_all(line):
        with db.connect() as conn:
            rows = db.open_approvals(conn)
        targets = rows if approve.is_submit_all(line) else [
            r for r in rows if r["ordinal"] == n
        ]
        if not targets:
            telegram.send(f"⚠️ Nothing open with number {n}." if n else
                          "Nothing is waiting for approval.")
            return True
        for row in targets:
            with db.connect() as conn:
                db.set_approval(conn, row["id"], "submit")
            asyncio.run(approve.submit_approved(profile, row, telegram))
        return True

    if (n := approve.parse_skip(line)) is not None:
        from .models import JobStatus

        with db.connect() as conn:
            for row in [r for r in db.open_approvals(conn) if r["ordinal"] == n]:
                db.set_approval(conn, row["id"], "skip")
                db.set_status(conn, row["dedupe_key"], JobStatus.SKIPPED)
        telegram.send(f"⏭ Dropped #{n}.")
        return True

    with db.connect() as conn:
        run = db.latest_proposal_run(conn)
    if run is None:
        return False

    if (mode := propose.parse_mode(line)) is not None:
        with db.connect() as conn:
            db.set_proposal_mode(conn, run["id"], mode.value)
        if mode is propose.Mode.AUTO:
            pairs = schedule.to_fill(run["id"], mode)
            if pairs:
                telegram.send(f"▶️ Filling {len(pairs)} from the last batch…")
                _fill(profile, telegram, pairs)
            else:
                telegram.send(
                    "Nothing is marked auto on the last batch — tap ✅ Auto "
                    "under a job (or `12 auto`) first."
                )
        else:
            telegram.send("📋 Noted — the list is yours.")
        return True

    if (bulk := propose.parse_bulk(line)) is not None:
        with db.connect() as conn:
            count = db.decide_remaining(conn, run["id"], bulk.value)
        telegram.send(f"{count} set to {bulk.value}.")
        return True

    if (decision := propose.parse_decision(line)) is not None:
        ordinal, choice = decision
        with db.connect() as conn:
            row = db.set_decision(conn, run["id"], ordinal, choice.value)
        if row is None:
            telegram.send(f"⚠️ No job numbered {ordinal} in the last batch.")
            return True
        with db.connect() as conn:
            job = db.job_by_key(conn, row["dedupe_key"])
        telegram.send(f"{ordinal} → {choice.value}")
        if job and choice is propose.Decision.MANUAL:
            schedule.send_outcome_prompt(telegram, job, telegram.jobs_chat_id)
        elif job and choice is propose.Decision.AUTO and run["mode"] == "auto":
            _fill(profile, telegram, [(row["id"], row["dedupe_key"])])
        return True

    return False


def _route_text(profile: Profile, telegram, text: str) -> None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    commands = [line for line in lines if _is_command(line)]
    if commands and len(commands) == len(lines):
        for line in lines:
            _route_command_line(profile, telegram, line)
        return

    # Not (only) commands: treat as form answers, with the usual guards.
    # apply_replies itself explains anything it could not match.
    from .run import apply_replies

    replies = parse_replies(text)
    if replies:
        apply_replies(replies, telegram)
    elif text.strip():
        telegram.send(
            "🤖 I'm listening, but I didn't recognize that. I understand "
            "job decisions (`12 auto`), approvals (`submit 3`), "
            "`window 3d`, and answers to questions I've asked."
        )


def _is_command(line: str) -> bool:
    return bool(
        propose.parse_since_command(line)
        or propose.is_decision_reply(line)
        or approve.is_approval_reply(line)
    )


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------


def listen(profile: Profile, minutes: int = 0, on_event=None) -> None:
    """Poll until stopped (minutes=0) or for a bounded test window."""
    telegram = Telegram.from_env()
    if telegram is None:
        raise RuntimeError("Telegram is not configured (.env).")

    deadline = time.time() + minutes * 60 if minutes else None
    offset = _load_offset()

    while deadline is None or time.time() < deadline:
        if batch_running():
            # The batch owns the conversation; do not even poll, or the
            # offsets race and her reply lands with the wrong consumer.
            time.sleep(20)
            continue

        texts, offset, callbacks = schedule._fetch_messages(telegram, offset)
        _save_offset(offset)

        for data, query_id in callbacks:
            if on_event:
                on_event(f"tap: {data[:40]}")
            try:
                _route_callback(profile, telegram, data, query_id)
            except Exception as exc:  # noqa: BLE001 - one bad tap must not kill the day
                telegram.send(f"⚠️ That tap failed: {type(exc).__name__}: {exc}")

        for text in texts:
            if on_event:
                on_event(f"text: {text[:40]!r}")
            try:
                _route_text(profile, telegram, text)
            except Exception as exc:  # noqa: BLE001
                telegram.send(f"⚠️ Could not handle that: {type(exc).__name__}: {exc}")

        time.sleep(POLL_SECONDS)
