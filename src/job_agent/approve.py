"""Review, approve, submit.

The agent fills an application, photographs it, and lists back the exact
values it put in each field. Nothing is sent until the user replies
`submit <n>`.

The part that makes the approval meaningful is `drift`: the form is filled
again at submit time, and if any value differs from the one she approved, the
submission is abandoned and she is told what changed. Approving is approving
*these values*, not this job — otherwise "approved" would degrade into a
rubber stamp on whatever the resolver happened to produce the second time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .resolve.engine import Answer, Resolution

# Values worth showing in full even when long; everything else is trimmed so
# a twelve-field form still fits in one Telegram message.
MAX_VALUE = 60

# The `approved` sentinel drift() uses for a field the review never showed.
NEW_FIELD = "<not shown to you>"


@dataclass
class Change:
    label: str
    approved: str
    current: str

    def __str__(self) -> str:
        return f"{self.label}: {self.approved!r} → {self.current!r}"


def snapshot(resolution: Resolution | None, fields: list | None = None) -> list[dict]:
    """The field->value list, in the order the form asks for it.

    Each entry carries the control's options too, so a value can be changed
    later by tapping a choice rather than retyping the exact wording.
    """
    if resolution is None:
        return []
    by_ref = {f.ref: f for f in (fields or [])}
    return [
        {
            "ref": a.ref,
            "label": a.label,
            "value": a.value,
            "options": getattr(by_ref.get(a.ref), "options", []),
            # The real control type, not one inferred from the option count.
            # Inferring it called a 13-month date dropdown a multi-select.
            "type": getattr(by_ref.get(a.ref), "type", a.field_type),
        }
        for a in resolution.answers
    ]


def dumps(values: list[dict[str, str]]) -> str:
    return json.dumps(values, ensure_ascii=False)


def loads(raw: str) -> list[dict[str, str]]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _by_label(values: list[dict]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for item in values:
        value = str(item.get("value") or "").strip()
        if value:
            grouped.setdefault(item["label"], []).append(value)
    for label in grouped:
        grouped[label].sort()
    return grouped


def drift(approved: list[dict], current: list[dict]) -> list[Change]:
    """Every way the form we are about to submit differs from the approved one.

    Compared by label, as a multiset of values, and not by element ref:
    Ashby regenerates its element ids on every visit, so an identical refill
    compared by ref reported all ten of its answers as simultaneously
    "field gone" and "not shown to you" — pure false drift, blocking a
    submission she had approved. A label can also legitimately appear twice
    (the month and year halves of one date), which the multiset absorbs.

    The trade-off, stated honestly: two same-labelled fields whose values
    swapped places would not be caught. The fill rules target those halves by
    their option lists, so a swap cannot come from the code paths that write
    them; a changed *value* still trips the guard.
    """
    approved_map = _by_label(approved)
    current_map = _by_label(current)

    changes: list[Change] = []
    for label, was in approved_map.items():
        now = current_map.get(label)
        if now is None:
            changes.append(Change(label, ", ".join(was), "<field gone>"))
        elif now != was:
            changes.append(Change(label, ", ".join(was), ", ".join(now)))

    for label, now in current_map.items():
        if label not in approved_map:
            changes.append(Change(label, NEW_FIELD, ", ".join(now)))

    return changes


def only_new_fields(changes: list[Change]) -> list[Change] | None:
    """The changes, iff every one is a field the review never showed.

    This is the split between the two kinds of drift. A value she approved
    that changed or vanished is a hard stop — the approval no longer covers
    the form. But a form that merely grew (a consent checkbox learned between
    review and submit) leaves everything she approved intact; that deserves a
    delta re-approval, not a dead end. Returns None when any approved value
    moved, so a mixed case stays a hard stop.
    """
    new = [c for c in changes if c.approved == NEW_FIELD]
    return new if changes and len(new) == len(changes) else None


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------


def _trim(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "(blank)"
    return value if len(value) <= MAX_VALUE else value[: MAX_VALUE - 1] + "…"


def format_review(
    ordinal: int,
    company: str,
    title: str,
    score: float,
    resume: str,
    url: str,
    values: list[dict[str, str]],
    unresolved: int = 0,
) -> str:
    total = len(values) + unresolved
    lines = [
        f"📝 #{ordinal} · {title} — {company}",
        f"match {score:.0%} · resume: {resume}",
        f"filled {len(values)} of {total} fields",
        "",
        "Filled:",
    ]
    for item in values:
        lines.append(f"  {item['label']}: {_trim(item['value'])}")
    if unresolved:
        lines.append(
            f"  ◻️ {unresolved} left blank (optional/unanswered) — add via "
            "✏️ or the window if you want them"
        )
    lines += [
        "",
        url,
        "",
        f"Reply `submit {ordinal}` to send it, or `skip {ordinal}` to drop it.",
    ]
    return "\n".join(lines)


def review_buttons(ordinal: int) -> list[list[tuple[str, str]]]:
    """Submit is the irreversible one, so it does not sit next to the others.

    "Open the window" exists because both confirmed submissions happened with
    her hand on the real form — she asked "where's Chrome?" when only a tap
    was offered. The tap tests the automation; the window is how she trusts.
    """
    return [
        [("✏️ Change an answer", f"a:{ordinal}:edit"), ("⏭ Skip", f"a:{ordinal}:skip")],
        [("🖥 Open the window — I'll click Submit myself", f"a:{ordinal}:window")],
        [("📤 Submit for me (automated)", f"a:{ordinal}:submit")],
    ]


def field_buttons(ordinal: int, values: list[dict]) -> list[list[tuple[str, str]]]:
    """One button per filled field, for picking which to change."""
    rows = []
    for index, item in enumerate(values[:20]):
        rows.append([(f"{item['label'][:28]}: {str(item['value'])[:14]}",
                      f"e:{ordinal}:{index}")])
    return rows


def value_buttons(
    ordinal: int, index: int, options: list[str], multi: bool = False,
    chosen: list[str] | None = None,
) -> list[list[tuple[str, str]]]:
    """Choices for replacing one answer.

    `multi` mirrors the question flow: where the form accepts several answers,
    editing has to accept several too, or a "select all that apply" field
    could only ever be corrected to a single value.
    """
    chosen = chosen or []
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for option_index, option in enumerate(options[:8]):
        mark = "☑️ " if multi and option in chosen else ("☐ " if multi else "")
        row.append((f"{mark}{option[:24]}", f"v:{ordinal}:{index}:{option_index}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if multi:
        rows.append([("✅ Done", f"v:{ordinal}:{index}:done")])
    return rows


def parse_review_callback(data: str) -> tuple[int, str] | None:
    """"a:3:submit" -> (3, "submit")."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "a":
        return None
    if parts[2] not in {"submit", "skip", "edit", "window"}:
        return None
    try:
        return int(parts[1]), parts[2]
    except ValueError:
        return None


def parse_field_callback(data: str) -> tuple[int, int] | None:
    """"e:3:5" -> approval 3, field 5."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "e":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def parse_value_callback(data: str) -> tuple[int, int, int | str] | None:
    """"v:3:5:2" -> approval 3, field 5, option 2. "v:3:5:done" -> …, "done"."""
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != "v":
        return None
    try:
        ordinal, index = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if parts[3] == "done":
        return ordinal, index, "done"
    try:
        return ordinal, index, int(parts[3])
    except ValueError:
        return None


_EDIT = re.compile(r"^\s*edit\s+#?(\d{1,3})\s+(.{1,200})$", re.I | re.S)


def parse_edit(text: str) -> tuple[int, str] | None:
    """"edit 3 San Jose" -> field 3, new value "San Jose"."""
    if match := _EDIT.match(text or ""):
        return int(match.group(1)), match.group(2).strip()
    return None


def format_delta(
    ordinal: int, company: str, title: str, changes: list[Change]
) -> str:
    """New-fields-only drift: show what grew, ask for the go-ahead again."""
    lines = [
        f"🆕 #{ordinal} · {title} — {company}",
        "The form now asks things your review never showed. "
        "I filled them, but nothing was sent:",
        "",
    ]
    lines += [f"  {c.label}: {_trim(c.current)}" for c in changes[:10]]
    if len(changes) > 10:
        lines.append(f"  …and {len(changes) - 10} more")
    lines += [
        "",
        f"Everything you already approved is unchanged. Reply "
        f"`submit {ordinal}` (or tap Submit) to send with these included, "
        f"`skip {ordinal}` to drop it, or ✏️ to change one.",
    ]
    return "\n".join(lines)


def format_drift(company: str, title: str, changes: list[Change]) -> str:
    return (
        f"🛑 Not submitting {title} — {company}.\n"
        "The form filled differently than what you approved:\n"
        + "\n".join(f"  {c}" for c in changes[:8])
        + "\n\nNothing was sent. Reply `submit` again after I re-show it."
    )


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

_SUBMIT = re.compile(r"^\s*submit\s+#?(\d{1,4})\s*$", re.I)
_SKIP = re.compile(r"^\s*(?:skip|drop)\s+#?(\d{1,4})\s*$", re.I)
_SUBMIT_ALL = re.compile(r"^\s*submit\s+all\s*$", re.I)


def parse_submit(text: str) -> int | None:
    if match := _SUBMIT.match(text or ""):
        return int(match.group(1))
    return None


def parse_skip(text: str) -> int | None:
    if match := _SKIP.match(text or ""):
        return int(match.group(1))
    return None


def is_submit_all(text: str) -> bool:
    return bool(_SUBMIT_ALL.match(text or ""))


def is_approval_reply(text: str) -> bool:
    """Anything this module owns, so other reply consumers leave it alone."""
    text = (text or "").strip()
    return bool(
        parse_submit(text) is not None
        or parse_skip(text) is not None
        or parse_edit(text) is not None
        or is_submit_all(text)
    )


def unresolved_count(resolution: Resolution | None) -> int:
    return len(resolution.unresolved) if resolution else 0


def confident(answers: list[Answer]) -> bool:
    return all(a.is_confident for a in answers)


# --------------------------------------------------------------------------
# executing an approval
# --------------------------------------------------------------------------


async def submit_approved(profile, approval_row, telegram=None, on_event=None) -> str:
    """Re-fill the approved application and submit it if nothing changed.

    Returns one of: "submitted", "delta", "drift", "gone", "failed".

    The form is filled again rather than submitted from a stale tab. A tab
    left open for an hour may have lost its session, and the ATS may have
    re-rendered the form; re-filling and then diffing is what makes the
    approval accurate rather than merely recent.
    """
    from .apply import apply_to_page
    from .browser import session as bs
    from .models import JobStatus
    from .run import _resume_for, _submit, RunReport, click_submit  # noqa: F401
    from .store import db
    from .tracker import Tracker, TrackerError

    with db.connect() as conn:
        job = db.job_by_key(conn, approval_row["dedupe_key"])
    if job is None:
        return "gone"

    approved = loads(approval_row["values_json"])
    resume = _resume_for(profile, job)

    session = await bs.attach()
    try:
        page = await session.open(job.url)
        outcome = await apply_to_page(
            page, profile, resume.path if resume else None, job=job
        )

        current = snapshot(outcome.resolution, outcome.fields)
        changes = drift(approved, current)
        if (new_only := only_new_fields(changes)) is not None:
            # The form grew but every value she approved is intact.
            # Abandoning here — the old behaviour — dead-ended a submission
            # she had asked for: a consent checkbox learned between review
            # and submit blocked the send with no way forward. Fold the new
            # fields into the snapshot, re-open the approval, and ask again;
            # her next `submit` is then checked against the fuller form. A
            # changed or vanished approved value still hard-stops below.
            if on_event:
                on_event(f"delta on {job.company}: {len(new_only)} new field(s)")
            with db.connect() as conn:
                db.record_submission(conn, approval_row["id"], "delta")
                db.reopen_approval(conn, approval_row["id"], dumps(current))
                db.set_status(conn, job.dedupe_key, JobStatus.PENDING_APPROVAL)
            if telegram:
                telegram.send(
                    format_delta(
                        approval_row["ordinal"], job.company, job.title, new_only
                    ),
                    buttons=review_buttons(approval_row["ordinal"]),
                )
            return "delta"
        if changes:
            if on_event:
                on_event(f"drift on {job.company}: {len(changes)} field(s)")
            if telegram:
                telegram.send(format_drift(job.company, job.title, changes))
            with db.connect() as conn:
                db.record_submission(conn, approval_row["id"], "drift")
                db.set_status(conn, job.dedupe_key, JobStatus.NEEDS_REVIEW)
            return "drift"

        report = RunReport()
        tracker: Tracker | None = None
        try:
            tracker = Tracker.connect()
        except TrackerError:
            tracker = None

        await _submit(
            job, outcome, page, tracker, telegram, report, resume,
            submit_mode="approved",
        )
        result = "submitted" if report.applied else "failed"
        with db.connect() as conn:
            db.record_submission(conn, approval_row["id"], result)
        return result
    finally:
        await session.close()
