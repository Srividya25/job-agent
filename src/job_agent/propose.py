"""Propose a batch of jobs and wait to be told what to do with them.

The scheduled runs never decide anything on their own. Each one discovers,
ranks, and hands the list back — with the match percentage and the URL for
every job — and every job waits for her explicit choice: auto / manual /
ignore. Nothing fills by default. (Until 2026-08-16 jobs at or above 70%
filled on the strength of the batch-level `auto` alone; she asked for that
to stop — the 70% line is now only a visual divider in the list.)

"Auto" here means *filled*, never submitted. submit_mode stays `never`, so
every application still waits for a human to press the button. The word is
the user's; the guarantee is the code's.

Ordinals are per-proposal and are what a Telegram reply refers to, the same
convention pending_questions already uses for form fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .models import Job, is_workday

# Display split only: the list groups "strong" (>=) from "weaker" (<) so a
# 25-job message stays scannable. No behavior hangs off this number — every
# job waits for her per-job decision regardless of score.
STRONG_MATCH = 0.70

TELEGRAM_LIMIT = 3900  # under the 4000 the client truncates at


class Decision(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"
    IGNORE = "ignore"


class Mode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


@dataclass
class Proposed:
    """One job on the list, with the number the user replies to."""

    ordinal: int
    job: Job
    tier: str  # "ask" (waits for her decision) or "yours" (Workday, hers by hand)

    @property
    def percent(self) -> int:
        return int(round(self.job.match_score * 100))


def build(jobs: list[Job]) -> list[Proposed]:
    """Number the ranked jobs and tier them.

    Every job is included regardless of score — the user asked to see
    everything her resume relates to, not only what clears a threshold — and
    every job is "ask": nothing fills unless she marks that job auto.

    Workday jobs are tier "yours" no matter what: Workday puts an account
    wall behind Apply, and she decided the agent never creates or holds
    those accounts. She still sees the job and its URL — applying is hers
    to do by hand.
    """
    return [
        Proposed(
            ordinal=i, job=job,
            tier="yours" if is_workday(job.ats, job.url) else "ask",
        )
        for i, job in enumerate(sorted(jobs, key=lambda j: -j.match_score), start=1)
    ]


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def age_label(job: Job) -> str:
    """How old the POSTING is — so an old listing that surfaced late is
    never mistaken for a fresh one. She judges staleness; nothing hides."""
    from datetime import date

    if not job.posted_at:
        return "posting date unknown"
    days = (date.today() - job.posted_at.date()).days
    if days <= 0:
        return "🆕 posted today"
    if days == 1:
        return "🆕 posted yesterday"
    if days <= 13:
        return f"posted {days}d ago"
    return f"⏳ posted {job.posted_at:%b %d}"


def _entry(item: Proposed, show_hint: bool) -> str:
    job = item.job
    resume = job.best_resume or "general"
    label = "resume" if item.job.match_score >= STRONG_MATCH else "nearest resume"
    lines = [
        f"{item.ordinal}. {item.percent}%  {job.company} · {job.title}",
        f"   {job.location or 'location not stated'} · {label}: {resume} "
        f"· {age_label(job)}",
        f"   {job.url}",
    ]
    if show_hint:
        lines.append(
            f"   → `{item.ordinal} auto` / `{item.ordinal} manual` "
            f"/ `{item.ordinal} ignore`"
        )
    return "\n".join(lines)


def format_proposal(items: list[Proposed], when: str, total_queued: int) -> list[str]:
    """Render the batch as Telegram-sized chunks.

    Returns a list of messages rather than one string: a batch of any size
    would otherwise be silently truncated at 4000 characters, which would cut
    the list off mid-job and lose URLs the user needs.
    """
    strong = [i for i in items if i.tier == "ask" and i.job.match_score >= STRONG_MATCH]
    weaker = [i for i in items if i.tier == "ask" and i.job.match_score < STRONG_MATCH]
    yours = [i for i in items if i.tier == "yours"]

    head = [
        f"🔎 {when} run — showing {len(items)} of {total_queued} queued",
        "",
        "You decide every job — and an auto pick starts filling right away:",
        "  `3 auto`    — I fill that one immediately, for your review",
        "  `3 manual`  — I skip it, you apply to it yourself",
        "  `3 ignore`  — drop it entirely",
        "",
        "Anything untouched is left alone.",
        "",
        "I never submit. Every form waits for you to press the button.",
    ]

    blocks: list[str] = ["\n".join(head)]

    if strong:
        blocks.append(
            "── strong matches · ≥70% ──"
            "\n\n" + "\n\n".join(_entry(i, show_hint=True) for i in strong)
        )
    if weaker:
        blocks.append(
            "── weaker matches · <70% ──\n\n"
            + "\n\n".join(_entry(i, show_hint=True) for i in weaker)
            + "\n\nShortcut: `rest ignore` drops everything undecided."
        )
    if yours:
        blocks.append(
            "── Workday · yours to apply — I don't touch Workday accounts ──"
            "\n\n" + "\n\n".join(_entry(i, show_hint=False) for i in yours)
        )
    if not items:
        blocks.append("Nothing queued. Nothing to decide.")

    return _chunk(blocks)


def skill_gaps(items: list[Proposed], top: int = 5) -> list[tuple[str, int]]:
    """The skills most often missing from her resume across this batch.

    `missing_skills` is computed per job by the scorer but was only visible
    one job at a time in `show`. Aggregated, it becomes actionable: a skill
    missing on a third of the batch is the single edit that would raise the
    most scores. Only repeats (>=2 jobs) are worth mentioning.
    """
    counts: dict[str, int] = {}
    for item in items:
        breakdown = item.job.match_breakdown
        if breakdown is None:
            continue
        # missing_skills also contains must-haves the POSTING never
        # mentions — that is the job differing from her wishlist, not a
        # resume gap. Only a skill the posting asks for counts here.
        text = f"{item.job.title}\n{item.job.description or ''}".lower()
        for skill in breakdown.missing_skills:
            if skill.lower() in text:
                counts[skill] = counts.get(skill, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(skill, n) for skill, n in ranked[:top] if n >= 2]


def format_skill_gaps(gaps: list[tuple[str, int]], total_jobs: int) -> str:
    lines = [
        "💡 Resume gaps across this batch — these skills appear in the "
        "postings but not on your resume:"
    ]
    lines += [f"  • {skill} — {n} of {total_jobs} jobs" for skill, n in gaps]
    lines.append(
        "Where they're true of you, adding them to the resume raises these "
        "match scores."
    )
    return "\n".join(lines)


def format_entries(items: list[Proposed]) -> list[str]:
    """Chunked messages of bare entries — the tail of a batch too long for
    one tappable message per job."""
    if not items:
        return []
    return _chunk(["\n\n".join(_entry(i, show_hint=True) for i in items)])


def _chunk(blocks: list[str]) -> list[str]:
    """Pack blocks into messages, never splitting a block across two."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > TELEGRAM_LIMIT:
            if current:
                messages.append(current)
                current = ""
            messages.extend(_split_entries(block))
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > TELEGRAM_LIMIT:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def _split_entries(block: str) -> list[str]:
    """Break an oversized block on entry boundaries, keeping each job whole."""
    out: list[str] = []
    current = ""
    for entry in block.split("\n\n"):
        candidate = f"{current}\n\n{entry}" if current else entry
        if len(candidate) > TELEGRAM_LIMIT and current:
            out.append(current)
            current = entry
        else:
            current = candidate
    if current:
        out.append(current)
    return out


# --------------------------------------------------------------------------
# buttons
# --------------------------------------------------------------------------
#
# callback_data is capped at 64 bytes by Telegram, so these stay terse:
#   d:<run>:<ordinal>:<decision>   one job
#   m:<run>:<mode>                 the go signal
#   b:<run>:<decision>             everything still undecided


def job_buttons(run_id: int, ordinal: int) -> list[list[tuple[str, str]]]:
    return [[
        ("✅ Auto", f"d:{run_id}:{ordinal}:auto"),
        ("✋ Manual", f"d:{run_id}:{ordinal}:manual"),
        ("✖️ Ignore", f"d:{run_id}:{ordinal}:ignore"),
    ]]


def control_buttons(run_id: int) -> list[list[tuple[str, str]]]:
    """Auto taps fill immediately, so there is no Start button — these only
    close out the batch."""
    return [
        [("✅ Done deciding", f"m:{run_id}:auto")],
        [("📋 All manual — just the list", f"m:{run_id}:manual")],
        [("✖️ Ignore everything undecided", f"b:{run_id}:ignore")],
    ]


def job_button_text(item: Proposed) -> str:
    """The one-job message a button row is attached to."""
    job = item.job
    default = (
        "Workday — yours to apply by hand"
        if item.tier == "yours"
        else "fills only if you tap Auto"
    )
    return (
        f"{item.ordinal}. {item.percent}%  {job.company}\n"
        f"{job.title}\n"
        f"{age_label(job)} · {job.location or 'location not stated'}\n"
        f"resume: {job.best_resume or 'general'} · {default}\n"
        f"{job.url}"
    )


# What became of a job she handles herself (manual picks, Workday):
#   o:<dedupe_key>:applied   she applied just now -> record in the Jobtracker
#   o:<dedupe_key>:dup       a repost she already applied to -> NO tracker row
#   o:<dedupe_key>:ignore    she looked and passed
# Keyed by dedupe_key rather than run/ordinal so the buttons stay valid long
# after the batch is over — she applies on her own schedule.

OUTCOMES = ("applied", "dup", "ignore")


def outcome_buttons(dedupe_key: str) -> list[list[tuple[str, str]]]:
    return [
        [
            ("✅ Applied", f"o:{dedupe_key}:applied"),
            ("♻️ Already applied", f"o:{dedupe_key}:dup"),
        ],
        [("✖️ Ignored", f"o:{dedupe_key}:ignore")],
    ]


def parse_outcome_callback(data: str) -> tuple[str, str] | None:
    """"o:abc123:applied" -> ("abc123", "applied")."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "o" or parts[2] not in OUTCOMES:
        return None
    return parts[1], parts[2]


def parse_callback(data: str) -> tuple[str, int, int | None, str] | None:
    """"d:4:2:auto" -> ("d", 4, 2, "auto"). Returns None if unrecognised."""
    parts = (data or "").split(":")
    try:
        if parts[0] == "d" and len(parts) == 4:
            return "d", int(parts[1]), int(parts[2]), parts[3]
        if parts[0] in {"m", "b"} and len(parts) == 3:
            return parts[0], int(parts[1]), None, parts[2]
    except (ValueError, IndexError):
        return None
    return None


def format_cli(items: list[Proposed]) -> str:
    """The same list for a terminal, without the reply syntax."""
    if not items:
        return "Nothing queued."
    lines = []
    for item in items:
        marker = "wday" if item.tier == "yours" else " ask"
        lines.append(
            f"[{marker}] {item.ordinal:>3}. {item.percent:>3}%  "
            f"{item.job.company[:20]:20} {item.job.title[:40]:40}"
        )
        lines.append(f"              {item.job.url}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# parsing replies
# --------------------------------------------------------------------------

# "auto", "manual", "mode auto", and the way she actually phrases it —
# "auto apply", "manual apply", "auto-apply". The first run of this lost a
# reply because only the bare word was accepted, and Telegram had already
# discarded it by the time anyone noticed.
_MODE = re.compile(
    r"^\s*(?:mode\s+)?(auto|manual)(?:\s*[-\s]?\s*apply(?:ing)?)?\s*$", re.I
)
# "window 3d" / "since 24h" / "window all" — sets the standing freshness
# window for future batches, straight from the phone.
_SINCE_CMD = re.compile(r"^\s*(?:window|since)\s+(\S{1,12})\s*$", re.I)
_BULK = re.compile(r"^\s*rest\s+(auto|manual|ignore)\s*$", re.I)
_PER_JOB = re.compile(r"^\s*#?(\d{1,3})\s*[.):\-]?\s+(auto|manual|ignore|skip)\s*$", re.I)


def parse_mode(text: str) -> Mode | None:
    """"auto" / "manual" / "mode auto" -> the batch mode."""
    if match := _MODE.match(text or ""):
        return Mode(match.group(1).lower())
    return None


def parse_since_command(text: str) -> str | None:
    """"window 3d" -> "3d". The value is validated where it is applied."""
    if match := _SINCE_CMD.match(text or ""):
        return match.group(1).lower()
    return None


def parse_bulk(text: str) -> Decision | None:
    """"rest ignore" -> apply that decision to everything undecided."""
    if match := _BULK.match(text or ""):
        return Decision(match.group(1).lower())
    return None


def parse_decision(text: str) -> tuple[int, Decision] | None:
    """"6 ignore" -> (6, IGNORE). "skip" is accepted as a synonym for ignore."""
    match = _PER_JOB.match(text or "")
    if not match:
        return None
    word = match.group(2).lower()
    return int(match.group(1)), Decision.IGNORE if word == "skip" else Decision(word)


def is_decision_reply(text: str) -> bool:
    """True for anything this module owns.

    run.py's consume_replies matches bare ordinals against open form
    questions. Without this guard a reply of "6 ignore" would be recorded as
    the *answer text* of whatever form field happened to be numbered 6 — and
    then cached and reused on future applications.
    """
    text = (text or "").strip()
    return bool(
        parse_mode(text) or parse_bulk(text) or parse_decision(text)
        # "window 3d" must never be cached as a form answer either.
        or parse_since_command(text)
    )
