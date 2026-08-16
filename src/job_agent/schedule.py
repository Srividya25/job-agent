"""The scheduled batch: discover, propose, wait for a decision, act.

Runs twice a day from launchd with no terminal attached, so it cannot prompt.
It sends the list to Telegram and then polls for the reply, which is the same
shape as the rest of the system — nothing blocks a browser or a board on a
human.

The one invariant: a batch with no answer does nothing at all. Silence is not
consent, so an unanswered run exits having filled nothing.

Reply polling deliberately keeps its own offset in `data/propose_offset` and
never writes `data/telegram_offset`. Telegram's getUpdates offset is a single
high-water mark and acknowledging it discards everything below, so if this
watcher advanced the shared one it would eat form answers that
run.consume_replies still needs.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from . import approve, propose
from .config import Profile, data_dir
from .models import Job, JobStatus
from .notify.telegram import API, Telegram
from .propose import Decision, Mode, Proposed
from .store import db

POLL_SECONDS = 20

# The scheduled proposal slots (matches the LaunchAgent's calendar).
SLOT_HOURS = (10, 15)


def missed_slot(
    last_started: datetime | None, now: datetime | None = None
) -> datetime | None:
    """The most recent scheduled slot no batch has covered, or None.

    launchd runs a slot missed during SLEEP on wake, but a slot missed while
    the machine was powered OFF is silently skipped. A login-time catch-up
    run calls this to decide whether it owes her a batch: it does when the
    latest past slot is newer than the last batch that actually started.
    """
    from datetime import time as _time, timedelta as _td

    now = now or datetime.now()
    past_slots = [
        datetime.combine(now.date() - _td(days=days), _time(hour))
        for days in (0, 1)
        for hour in SLOT_HOURS
        if datetime.combine(now.date() - _td(days=days), _time(hour)) <= now
    ]
    latest = max(past_slots)
    if last_started is None or last_started < latest:
        return latest
    return None


@dataclass
class BatchResult:
    run_id: int = 0
    mode: Mode | None = None
    proposed: int = 0
    filled: int = 0
    ignored: int = 0
    manual: int = 0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# offsets
# --------------------------------------------------------------------------


def _offset_path() -> Path:
    return data_dir() / "propose_offset"


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


def _window_path() -> Path:
    return data_dir() / "batch_window"


def load_window() -> str:
    """The standing freshness window ("3d", "24h", …); "" = no window."""
    try:
        value = _window_path().read_text().strip()
        return "" if value in {"", "all", "any"} else value
    except OSError:
        return ""


def save_window(value: str) -> None:
    _window_path().write_text(value.strip())


def apply_since_command(telegram, raw: str) -> bool:
    """Handle a typed "window 3d" from any listener. Returns True if valid.

    Validated with the same parser the CLI uses, so the vocabulary is one
    list: 24h, 3d, 1w, 30d, all, or bare hours/days/weeks like 12h or 2w.
    """
    from .models import parse_since

    try:
        hours = parse_since(raw)
    except ValueError as exc:
        if telegram:
            telegram.send(f"⚠️ {exc}")
        return False
    save_window("" if hours == 0 else raw)
    if telegram:
        telegram.send(
            "🗓 Got it — batches now show every queued job."
            if hours == 0 else
            f"🗓 Got it — from the next batch on, only jobs from the last "
            f"{raw}. Send `window all` to clear."
        )
    return True


def _fetch_messages(
    telegram: Telegram, offset: int,
    documents: list[tuple[dict, str]] | None = None,
) -> tuple[list[str], int, list[tuple[str, str]]]:
    """Texts and button taps since `offset`, plus the new offset.

    Returns (texts, next_offset, callbacks) where each callback is
    (callback_data, callback_query_id). Typed replies still work — the
    buttons are an easier path to the same decisions, not a replacement.

    `documents`, when given, collects (document dict, caption) for files the
    user sent — how a new resume arrives by phone. Callers that don't pass
    it simply skip file messages, as they always did.
    """
    try:
        response = httpx.get(
            f"{API}/bot{telegram.token}/getUpdates",
            params={
                "offset": offset,
                "timeout": 0,
                "allowed_updates": '["message","callback_query"]',
            },
            timeout=20.0,
        )
        if response.status_code != 200:
            return [], offset, []
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return [], offset, []

    texts: list[str] = []
    callbacks: list[tuple[str, str]] = []
    next_offset = offset
    for update in payload.get("result", []):
        next_offset = max(next_offset, update.get("update_id", 0) + 1)

        if query := update.get("callback_query"):
            chat = ((query.get("message") or {}).get("chat") or {})
            if str(chat.get("id")) not in telegram.allowed_chats:
                continue
            callbacks.append((query.get("data") or "", query.get("id") or ""))
            continue

        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id")) not in telegram.allowed_chats:
            continue
        if text := (message.get("text") or "").strip():
            texts.append(text)
        elif (doc := message.get("document")) and documents is not None:
            documents.append((doc, (message.get("caption") or "").strip()))
    return texts, next_offset, callbacks


# --------------------------------------------------------------------------
# the batch
# --------------------------------------------------------------------------


# How many jobs get their own tappable message. Beyond this the batch would
# flood the chat (and Telegram's rate limits) with hundreds of messages, so
# the rest travel as a compact ranked list answered by typing `N auto`.
BUTTON_CAP = 40


def open_batch(
    jobs: list[Job], label: str, total_queued: int, telegram: Telegram | None
) -> tuple[int, list[Proposed]]:
    """Record the batch and send it out. Returns (run_id, items).

    Everything goes to the jobs channel (`telegram.jobs_chat_id`) — the main
    chat stays for form questions, reviews, and submissions. With no second
    chat configured, the two are the same and nothing changes.
    """
    items = propose.build(jobs)

    with db.connect() as conn:
        run_id = db.start_proposal(conn, label)
        for item in items:
            db.add_proposal(
                conn, run_id, item.ordinal, item.job.dedupe_key,
                item.job.match_score, item.tier,
            )

    if telegram:
        jobs_chat = telegram.jobs_chat_id
        telegram.send(
            f"🔎 {label} run — {len(items)} of {total_queued} queued\n\n"
            "Tap Auto, Manual, or Ignore under each job, then Start. "
            "Nothing fills unless you mark it Auto; anything untouched is "
            "left alone.\n\n"
            "I never submit — every form waits for you to press the button.",
            chat_id=jobs_chat,
        )
        # One message per job, because Telegram attaches buttons to a message.
        # Workday jobs get outcome buttons instead of decision buttons: there
        # is no auto/manual decision — they are hers by hand — but what
        # became of them (applied / repost / ignored) is worth a tap.
        for item in items[:BUTTON_CAP]:
            telegram.send(
                propose.job_button_text(item),
                buttons=(
                    propose.outcome_buttons(item.job.dedupe_key)
                    if item.tier == "yours"
                    else propose.job_buttons(run_id, item.ordinal)
                ),
                chat_id=jobs_chat,
            )
        if overflow := items[BUTTON_CAP:]:
            for chunk in propose.format_entries(overflow):
                telegram.send(chunk, chat_id=jobs_chat)
            telegram.send(
                f"({len(overflow)} more above without buttons — reply "
                "`N auto` / `N manual` / `N ignore` by number for those.)",
                chat_id=jobs_chat,
            )
        telegram.send(
            "When you're ready:", buttons=propose.control_buttons(run_id),
            chat_id=jobs_chat,
        )
        if gaps := propose.skill_gaps(items):
            telegram.send(
                propose.format_skill_gaps(gaps, len(items)), chat_id=jobs_chat
            )
    return run_id, items


def collect(
    run_id: int,
    telegram: Telegram,
    wait_minutes: int,
    on_event=None,
) -> Mode | None:
    """Poll for the mode and per-job decisions until the batch is settled.

    Returns the chosen mode, or None if the window closed unanswered.
    """
    deadline = time.time() + wait_minutes * 60
    offset = _load_offset()
    mode: Mode | None = None

    while time.time() < deadline:
        texts, offset, callbacks = _fetch_messages(telegram, offset)
        _save_offset(offset)

        for data, query_id in callbacks:
            parsed = propose.parse_callback(data)
            if parsed is None or parsed[1] != run_id:
                # Question and outcome taps are valid here too — she answers
                # whenever she is at her phone, not when the "right" listener
                # runs.
                if not handle_common_callback(telegram, data, query_id):
                    telegram.answer_callback(query_id, "That batch is over.")
                continue
            kind, _, ordinal, value = parsed

            if kind == "d" and ordinal is not None:
                with db.connect() as conn:
                    row = db.set_decision(conn, run_id, ordinal, value)
                telegram.answer_callback(
                    query_id,
                    f"{ordinal} → {value}" if row else f"No job {ordinal}",
                )
                if on_event:
                    on_event(f"tap: {ordinal} -> {value}")
                # A manual pick is hers to do — hand her the report-back
                # buttons right away, so the applied/repost/ignored outcome
                # has somewhere to land whenever she gets to it.
                if row is not None and value == Decision.MANUAL.value:
                    with db.connect() as conn:
                        job = db.job_by_key(conn, row["dedupe_key"])
                    if job is not None:
                        send_outcome_prompt(
                            telegram, job, chat_id=telegram.jobs_chat_id
                        )
            elif kind == "b":
                with db.connect() as conn:
                    n = db.decide_remaining(conn, run_id, value)
                telegram.answer_callback(query_id, f"{n} set to {value}")
                if on_event:
                    on_event(f"tap: rest -> {value} ({n})")
            elif kind == "m":
                mode = Mode(value)
                with db.connect() as conn:
                    db.set_proposal_mode(conn, run_id, mode.value)
                telegram.answer_callback(query_id, f"Starting — {mode.value}")
                if on_event:
                    on_event(f"tap: mode = {mode.value}")
                return mode

        # One message often carries several instructions:
        #
        #     4 auto
        #     rest ignore
        #     auto
        #
        # telegram.parse_replies learned this same lesson for form answers.
        # Treating the message as one string matched none of these lines and
        # dropped the lot.
        lines = [
            line.strip()
            for text in texts
            for line in text.splitlines()
            if line.strip()
        ]

        for text in lines:
            if (found := propose.parse_mode(text)) is not None:
                mode = found
                with db.connect() as conn:
                    db.set_proposal_mode(conn, run_id, mode.value)
                if on_event:
                    on_event(f"mode = {mode.value}")
                # The mode word is the go signal, not just an answer: any
                # per-job overrides she wanted have been sent before it.
                # Waiting past this point for the <70% jobs to be decided
                # would stall a batch she has already told me to run.
                return mode

            if (bulk := propose.parse_bulk(text)) is not None:
                with db.connect() as conn:
                    n = db.decide_remaining(conn, run_id, bulk.value)
                if on_event:
                    on_event(f"{n} remaining -> {bulk.value}")
                continue

            if (parsed := propose.parse_decision(text)) is not None:
                ordinal, decision = parsed
                with db.connect() as conn:
                    row = db.set_decision(conn, run_id, ordinal, decision.value)
                if on_event:
                    on_event(
                        f"{ordinal} -> {decision.value}"
                        if row
                        else f"no job numbered {ordinal} in this batch"
                    )
                if row is None:
                    telegram.send(f"⚠️ No job numbered {ordinal} in this batch.")
                elif decision is Decision.MANUAL:
                    with db.connect() as conn:
                        job = db.job_by_key(conn, row["dedupe_key"])
                    if job is not None:
                        send_outcome_prompt(
                            telegram, job, chat_id=telegram.jobs_chat_id
                        )
                continue

            if (window := propose.parse_since_command(text)) is not None:
                apply_since_command(telegram, window)
                continue

            # Anything else. Polling has already advanced the offset, so this
            # message is gone from Telegram — saying nothing would leave her
            # waiting on a batch that silently ignored her. This happened on
            # the first real run.
            if on_event:
                on_event(f"unrecognised reply: {text[:40]!r}")
            if mode is None:
                telegram.send(
                    f"🤔 I didn't understand “{text[:60]}”.\n\n"
                    "Reply exactly `auto` (I fill them for you) or `manual` "
                    "(you apply yourself)."
                )

        if mode is not None and _settled(run_id):
            return mode
        time.sleep(POLL_SECONDS)

    return mode


def _settled(run_id: int) -> bool:
    """True once every job that needs a decision has one."""
    with db.connect() as conn:
        rows = db.proposals_for(conn, run_id)
    return not [r for r in rows if r["tier"] == "ask" and r["decision"] is None]


def to_fill(run_id: int, mode: Mode) -> list[tuple[int, str]]:
    """(proposal_id, dedupe_key) for every job this batch should fill.

    In manual mode this is empty by construction — the user asked for the
    list, not the work.
    """
    if mode is not Mode.MANUAL:
        with db.connect() as conn:
            rows = db.proposals_for(conn, run_id)
        return [
            (r["id"], r["dedupe_key"])
            for r in rows
            # Only an explicit per-job `auto` fills — no score fills by
            # default (her 2026-08-16 change; tier "auto" rows from batches
            # recorded before then get no grandfathered default either).
            # Workday ("yours") never fills, even against a typed `N auto` —
            # the portal needs an account the agent must not hold.
            if r["tier"] != "yours"
            and r["decision"] == Decision.AUTO.value
        ]
    return []


async def fill_batch(
    profile: Profile,
    pairs: list[tuple[int, str]],
    telegram: Telegram | None,
    on_event=None,
) -> int:
    """Fill each chosen application. Never submits.

    submit_mode is forced to "never" here rather than read from the profile:
    this path exists because the user chose "fill and leave it for me", and
    that promise should not silently change if the profile is edited later.
    """
    from .browser.session import BrowserNotRunning
    from .run import apply_batch

    if not pairs:
        return 0

    # One call, one browser session. Opening playwright per job made a batch
    # of ten take minutes of pure startup.
    keys = [key for _, key in pairs]

    async def on_filled(job, outcome, page) -> None:
        """Photograph the filled form and list its values back for approval."""
        # Pass the fields too: without them the snapshot carries no options,
        # so "Change an answer" had nothing to offer as buttons and a
        # multi-select field could not be re-answered at all.
        values = approve.snapshot(outcome.resolution, outcome.fields)
        # Only values whose write actually landed. Six fields once failed
        # with "element not found" on every fill while the review presented
        # them as filled — she approved a form that did not exist.
        if outcome.fill is not None:
            written = {w.ref for w in outcome.fill.written}
            values = [v for v in values if v["ref"] in written]
        with db.connect() as conn:
            ordinal = db.next_approval_ordinal(conn)
            db.add_approval(conn, ordinal, job.dedupe_key, approve.dumps(values))
            db.set_status(conn, job.dedupe_key, JobStatus.PENDING_APPROVAL)

        if telegram is None:
            return
        telegram.send(
            approve.format_review(
                ordinal, job.company, job.title, job.match_score,
                job.best_resume or "general", job.url, values,
                approve.unresolved_count(outcome.resolution),
            ),
            buttons=approve.review_buttons(ordinal),
        )
        try:
            telegram.send_photo(await page.screenshot(full_page=False))
        except Exception:  # noqa: BLE001 - the values already went out
            pass

    try:
        report = await apply_batch(
            profile,
            limit=len(keys),
            submit_mode="never",
            min_score=0.0,
            only=keys,
            on_event=on_event,
            on_filled=on_filled,
        )
    except Exception as exc:  # noqa: BLE001 - a browser fault must not strand the batch
        # Originally this caught only BrowserNotRunning, and a Playwright
        # protocol error on attach took the whole run down with a stack trace
        # — after she had already chosen the jobs. Her decisions are recorded
        # and the jobs are still queued, so the recoverable thing is to say
        # what broke and leave the batch replayable.
        detail = (
            str(exc) if isinstance(exc, BrowserNotRunning)
            else f"{type(exc).__name__}: {exc}"
        )
        note = (
            f"🟠 You chose {len(keys)}, but I could not open the browser.\n\n"
            f"{detail[:600]}\n\n"
            "Nothing was filled. Retry with:  job-agent batch --no-discover"
        )
        if on_event:
            on_event(note)
        if telegram:
            telegram.send(note)
        return 0
    if on_event:
        for err in report.errors[:5]:
            on_event(err)

    # If anything got stuck on questions, she may be at her phone right now:
    # hold briefly for her taps, then refill the same applications at once
    # instead of abandoning them until the next scheduled run.
    if report.stuck and telegram is not None:
        with db.connect() as conn:
            marks = ",".join("?" for _ in keys)
            still_open = conn.execute(
                f"select count(*) from pending_questions "
                f"where answered_at is null and dedupe_key in ({marks})", keys,
            ).fetchone()[0]
        if still_open:
            telegram.send(
                "⏸ Holding 5 more minutes for the remaining question(s) above."
            )
        if hold_for_answers(keys, telegram, minutes=5, on_event=on_event):
            report = await apply_batch(
                profile, limit=len(keys), submit_mode="never", min_score=0.0,
                only=keys, on_event=on_event, on_filled=on_filled,
            )

    with db.connect() as conn:
        for proposal_id, _ in pairs:
            db.mark_acted(conn, proposal_id)

    # apply_batch counts a filled-but-unsubmitted job as "skipped" under
    # submit_mode=never; what matters here is how many were not left stuck.
    return max(0, len(keys) - report.stuck - len(report.errors))


def record_outcome(telegram, query_id: str, dedupe_key: str, outcome: str) -> None:
    """She told us what became of a job she handled herself.

    "applied" writes the Jobtracker row — the one path that records a
    hand-application she made outside a held-open window. "dup" (a repost of
    something she already applied to) deliberately does NOT: the tracker
    already has the original, and a second row would double-count it. Both
    mark the job applied here so it is never proposed again; "ignore" drops
    it the same way an ignore decision would.
    """
    from .models import JobStatus

    with db.connect() as conn:
        job = db.job_by_key(conn, dedupe_key)
    if job is None:
        telegram.answer_callback(query_id, "I don't know that job any more.")
        return
    if job.status is JobStatus.APPLIED and outcome != "ignore":
        telegram.answer_callback(query_id, "Already recorded — all good.")
        return

    if outcome == "ignore":
        with db.connect() as conn:
            db.set_status(conn, dedupe_key, JobStatus.SKIPPED)
        telegram.answer_callback(query_id, f"Dropped {job.company}.")
        return

    with db.connect() as conn:
        db.set_status(conn, dedupe_key, JobStatus.APPLIED)

    if outcome == "dup":
        telegram.answer_callback(query_id, "Marked as already applied.")
        telegram.send(
            f"♻️ {job.title} — {job.company}: marked applied from before. "
            "I won't propose it again, and I did NOT add a new Jobtracker "
            "row — the original application is the record."
        )
        return

    # outcome == "applied" — she applied just now; the tracker gets a row.
    note = "Applied by hand (reported via Telegram)."
    recorded = ""
    try:
        from .config import load_profile
        from .run import _resume_for
        from .tracker import Tracker, TrackerError

        try:
            resume = _resume_for(load_profile(), job)
        except Exception:  # noqa: BLE001 - a missing profile must not lose the tap
            resume = None
        try:
            Tracker.connect().record(
                company=job.company, job_title=job.title, job_url=job.url,
                job_description=job.description or "",
                resume_label=job.best_resume,
                tracker_file=resume.tracker_file if resume else "",
                note=note,
            )
            recorded = "Recorded in your Jobtracker."
        except TrackerError as exc:
            recorded = f"⚠️ Jobtracker not updated: {exc}"
    except Exception as exc:  # noqa: BLE001 - report, never crash a listener
        recorded = f"⚠️ Jobtracker not updated: {exc}"

    telegram.answer_callback(query_id, f"Applied — {job.company} ✅")
    telegram.send(
        f"✅ {job.title} — {job.company}: marked applied. {recorded}\n"
        "Won't be proposed again, even if the posting comes back."
    )


def handle_common_callback(telegram, data: str, query_id: str) -> bool:
    """Taps that are valid in ANY listener: question answers and outcomes.

    Every polling loop routes through this first, because a tap arrives
    whenever she is at her phone — not when the right specialized listener
    happens to be running. A Submit tap was once swallowed for exactly this
    reason.
    """
    from .notify.telegram import parse_question_callback

    if (picked := parse_question_callback(data)) is not None:
        record_question_tap(telegram, query_id, *picked)
        return True
    if (outcome := propose.parse_outcome_callback(data)) is not None:
        record_outcome(telegram, query_id, *outcome)
        return True
    return False


def send_outcome_prompt(telegram, job, chat_id: str | None = None) -> None:
    """The buttons she reports back with once a job is hers to handle."""
    telegram.send(
        f"When you've handled it, tell me — {job.title} — {job.company}:\n"
        f"{job.url}",
        buttons=propose.outcome_buttons(job.dedupe_key),
        chat_id=chat_id,
    )


def hand_done_signalled() -> bool:
    """Whether `done` was given outside Telegram.

    She typed `done` into the CLI once and the hold — listening only to
    Telegram — sat there until timeout. Any channel that can touch this file
    (the CLI, the assistant, a script) can now end a hold: the file is the
    signal, and consuming it deletes it.
    """
    flag = data_dir() / "hand_done"
    if flag.exists():
        try:
            flag.unlink()
        except OSError:
            pass
        return True
    return False


def record_question_tap(
    telegram, query_id: str, pending_id: int, index: int | str
) -> None:
    """Store a tapped choice, and cache it for every later form.

    The tap carries an index rather than the option text, so the exact wording
    the form expects is read back from storage — not re-typed and not guessed.

    On a "select all that apply" question taps accumulate and the answer is
    only finalized by Done. Replacing on every tap meant a five-option
    question went out carrying whichever one was tapped last.
    """
    import json

    from .notify.telegram import MULTI_SEP, is_multi_select, question_buttons
    from .resolve import cache as answer_cache

    with db.connect() as conn:
        row = db.pending_by_id(conn, pending_id)
        if row is None or row["answered_at"]:
            telegram.answer_callback(query_id, "Already answered.")
            return
        options = json.loads(row["options"] or "[]")
        question = row["full_question"] or row["question"]
        multi = is_multi_select(row["field_type"] or "text", options, question)
        chosen = [c for c in (row["answer"] or "").split(MULTI_SEP) if c]

        if index == "done":
            if not chosen:
                telegram.answer_callback(query_id, "Pick at least one first.")
                return
            answer = MULTI_SEP.join(chosen)
            db.answer_pending(conn, pending_id, answer)
            db.set_status(conn, row["dedupe_key"], JobStatus.NEW)
        elif not isinstance(index, int) or index >= len(options):
            telegram.answer_callback(query_id, "That choice is gone.")
            return
        elif multi:
            option = options[index]
            # Toggle, so a mistaken tap can be undone.
            if option in chosen:
                chosen.remove(option)
            else:
                chosen.append(option)
            conn.execute(
                "update pending_questions set answer = ? where id = ?",
                (MULTI_SEP.join(chosen), pending_id),
            )
            telegram.answer_callback(
                query_id, f"{len(chosen)} selected — tap Done when finished"
            )
            telegram.send(
                f"Selected so far for “{question[:44]}”:\n"
                + ("\n".join(f"  • {c}" for c in chosen) or "  (none)"),
                buttons=question_buttons(
                    pending_id, options, multi=True, chosen=chosen
                ),
            )
            return
        else:
            answer = options[index]
            db.answer_pending(conn, pending_id, answer)
            db.set_status(conn, row["dedupe_key"], JobStatus.NEW)

    answer_cache.remember(
        row["question"], answer, row["scope"] or "*", row["field_type"] or "text"
    )
    telegram.answer_callback(query_id, f"Saved: {answer[:40]}")
    telegram.send(f"✅ Recorded for “{question[:50]}”:\n{answer}")


def _offer_new_value(telegram, query_id: str, ordinal: int, index: int) -> None:
    """She picked which answer to change; offer the form's own choices."""
    from . import approve

    with db.connect() as conn:
        row = db.approval_by_ordinal(conn, ordinal)
    if row is None:
        telegram.answer_callback(query_id, "That review is closed.")
        return
    values = approve.loads(row["values_json"])
    if index >= len(values):
        telegram.answer_callback(query_id, "No such field.")
        return

    from .notify.telegram import MULTI_SEP, is_multi_select

    item = values[index]
    options = item.get("options") or []
    multi = is_multi_select(item.get("type", "text"), options, item["label"])
    chosen = [c for c in str(item["value"]).split(MULTI_SEP) if c] if multi else []
    telegram.answer_callback(query_id, "Pick the new value")
    telegram.send(
        f"✏️ {item['label']}\ncurrently: {item['value']}\n\n"
        + (
            "Tap all that apply, then Done."
            if multi
            else "Tap the new value below."
            if options
            else f"Reply `edit {index + 1} <new value>` to change it."
        ),
        buttons=(
            approve.value_buttons(ordinal, index, options, multi=multi, chosen=chosen)
            if options
            else None
        ),
    )


def _apply_edit(
    profile, telegram, query_id: str, ordinal: int, index: int,
    option_index: int | str,
) -> None:
    from . import approve
    from .notify.telegram import MULTI_SEP, is_multi_select

    with db.connect() as conn:
        row = db.approval_by_ordinal(conn, ordinal)
    if row is None:
        telegram.answer_callback(query_id, "That review is closed.")
        return
    values = approve.loads(row["values_json"])
    if index >= len(values):
        telegram.answer_callback(query_id, "No such field.")
        return

    item = values[index]
    options = item.get("options") or []
    multi = is_multi_select(item.get("type", "text"), options, item["label"])

    if option_index == "done":
        telegram.answer_callback(query_id, "Updating…")
        _change_answer(profile, telegram, row, index, str(item["value"]))
        return

    if not isinstance(option_index, int) or option_index >= len(options):
        telegram.answer_callback(query_id, "That choice is gone.")
        return

    if not multi:
        telegram.answer_callback(query_id, "Updating…")
        _change_answer(profile, telegram, row, index, options[option_index])
        return

    # Multi-select: accumulate in the snapshot until Done, so a form that
    # takes several answers can be corrected to several answers.
    option = options[option_index]
    chosen = [c for c in str(item["value"]).split(MULTI_SEP) if c]
    if option in chosen:
        chosen.remove(option)
    else:
        chosen.append(option)
    item["value"] = MULTI_SEP.join(chosen)
    with db.connect() as conn:
        conn.execute(
            "update approvals set values_json = ? where id = ?",
            (approve.dumps(values), row["id"]),
        )
    telegram.answer_callback(query_id, f"{len(chosen)} selected — tap Done")
    telegram.send(
        f"✏️ {item['label']}\nselected: " + (", ".join(chosen) or "(none)"),
        buttons=approve.value_buttons(
            ordinal, index, options, multi=True, chosen=chosen
        ),
    )


def _change_answer(profile, telegram, row, index: int, new_value: str) -> None:
    """Record the correction, re-fill, and ask again.

    The cache is updated rather than the snapshot: the point is that the form
    fills this way next time too. Re-filling afterwards is what keeps the
    approval honest — she approves what would actually be submitted, and the
    drift check at submit time still has something true to compare against.
    """
    from . import approve, schedule
    from .resolve import cache as answer_cache
    from .resolve.engine import scope_for

    values = approve.loads(row["values_json"])
    item = values[index]

    with db.connect() as conn:
        job = db.job_by_key(conn, row["dedupe_key"])
    answer_cache.remember(
        item["label"], new_value, scope_for(job.url) if job else "*", "text"
    )
    telegram.send(f"✏️ {item['label']} → {new_value}\n\nRe-filling…")

    with db.connect() as conn:
        db.set_approval(conn, row["id"], "superseded")
        db.set_status(conn, row["dedupe_key"], JobStatus.NEW)
        pair = [(0, row["dedupe_key"])]
    asyncio.run(schedule.fill_batch(profile, pair, telegram))



def hold_for_answers(
    keys: list[str], telegram: Telegram, minutes: int = 10, on_event=None
) -> bool:
    """Wait briefly for her taps so a stuck application can continue NOW.

    Before this, a stuck fill was parked and only retried on a later run —
    even when she was at her phone and answered within a minute. The hold
    polls for question taps (single consumer: no watcher runs during a
    batch), and returns True the moment every question for these jobs is
    answered, so the caller can refill immediately while she is still there.
    """
    deadline = time.time() + minutes * 60
    offset = _load_offset()

    def open_count() -> int:
        with db.connect() as conn:
            marks = ",".join("?" for _ in keys)
            return conn.execute(
                f"select count(*) from pending_questions "
                f"where answered_at is null and dedupe_key in ({marks})",
                keys,
            ).fetchone()[0]

    if open_count() == 0:
        return True

    while time.time() < deadline:
        texts, offset, callbacks = _fetch_messages(telegram, offset)
        _save_offset(offset)
        for data, query_id in callbacks:
            if handle_common_callback(telegram, data, query_id):
                if on_event:
                    on_event(f"tap: {data[:20]}")
            else:
                telegram.answer_callback(query_id, "Not expecting that right now.")
        # Typed answers count here exactly as taps do — fetching advanced
        # the offset, so an unapplied answer is an eaten answer.
        for text in texts:
            from .notify.telegram import parse_replies
            from .run import apply_replies

            apply_replies(parse_replies(text), telegram)
        remaining = open_count()
        if remaining == 0:
            if on_event:
                on_event("all questions answered — continuing")
            return True
        time.sleep(POLL_SECONDS)
    return False


async def window_session(
    profile: Profile, dedupe_key: str, telegram: Telegram | None,
    minutes: int = 45, on_event=None,
) -> int:
    """Fill an application and hold the window open for her hands.

    The core of `with-me`, extracted so a review's "Open the window" button
    can run it too. Fills what it can, says what it could not, keeps the
    browser up until `done` (Telegram or the hand_done flag) or timeout, then
    captures and learns whatever she typed. Returns the learned count.
    """
    import time as _time

    from .apply import apply_to_page, form_root
    from .browser import session as bs
    from .fill.verify import read_answer
    from .resolve import cache as answer_cache
    from .resolve.engine import scope_for
    from .resolve.rules import _control_unit
    from .run import _resume_for

    with db.connect() as conn:
        job = db.job_by_key(conn, dedupe_key)
    if job is None:
        return 0

    session = await bs.attach()
    try:
        resume = _resume_for(profile, job)
        page = await session.open(job.url)
        await page.wait_for_timeout(2500)
        outcome = await apply_to_page(
            page, profile, resume.path if resume else None, job=job
        )
        filled = len(outcome.resolution.answers) if outcome.resolution else 0
        blanks = list(outcome.resolution.unresolved) if outcome.resolution else []
        blanks += [w for w in (outcome.fill.failed if outcome.fill else [])]

        if telegram:
            note = (
                f"🖥 Window open: {job.title} — {job.company}\n"
                f"Filled {filled}. "
                + (
                    "Left for you:\n"
                    + "\n".join(f"  • {getattr(b, 'question', '') or b.label}"[:64]
                                  for b in blanks[:10])
                    if blanks else "Nothing left blank."
                )
                + "\n\nCheck it, click Submit yourself, then reply `done`."
            )
            telegram.send(note)

        deadline = _time.time() + minutes * 60
        offset = _load_offset()
        done = False
        while _time.time() < deadline and not done:
            if telegram:
                texts, offset, callbacks = _fetch_messages(telegram, offset)
                _save_offset(offset)
                for data, query_id in callbacks:
                    handle_common_callback(telegram, data, query_id)
                for text in texts:
                    from .notify.telegram import parse_replies
                    from .run import apply_replies

                    apply_replies(parse_replies(text), telegram)
                done = any(t.strip().lower() in {"done", "finished", "ok done"}
                           for t in texts)
            done = done or hand_done_signalled()
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
                    on_event(f"learned: {field_.label[:38]} = {value[:28]}")
        if telegram:
            telegram.send(f"✅ Window closed — learned {learned} answer(s). "
                          "Tell me if you submitted and I'll record it.")
        return learned
    finally:
        await session.close()


def summarize(result: BatchResult, telegram: Telegram | None) -> str:
    if result.mode is None:
        text = (
            "⏳ No answer, so I did nothing. The list stays queued — "
            "reply `auto` or `manual` any time and I'll pick it up on the "
            "next run."
        )
    elif result.mode is Mode.MANUAL:
        text = (
            f"📋 Manual: {result.proposed} jobs are yours. "
            "Nothing was filled."
        )
    else:
        text = (
            f"✅ Filled {result.filled} of {result.proposed} — "
            "every one is waiting for you to review and press submit.\n"
            f"⏭ {result.ignored} ignored · {result.manual} left for you"
        )
    if telegram:
        telegram.send(text, chat_id=telegram.jobs_chat_id)
    return text


def tally(run_id: int, result: BatchResult) -> BatchResult:
    with db.connect() as conn:
        rows = db.proposals_for(conn, run_id)
    result.proposed = len(rows)
    result.ignored = len([r for r in rows if r["decision"] == Decision.IGNORE.value])
    result.manual = len([r for r in rows if r["decision"] == Decision.MANUAL.value])
    return result


def label_for(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return now.strftime("%-I:%M %p").lower()
