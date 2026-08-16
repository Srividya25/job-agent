"""Tier 1: remember answers so the same question is never asked twice.

Keyed on a normalized hash of the question text, so these all collide onto
one entry, which is the point:

    "Why do you want to work here? *"
    "Why do you want to work here?"
    "  why do you WANT to work here  "

Two scopes are stored. A global answer applies everywhere; a portal-scoped
answer overrides it when one company words a question the same but wants a
different reply. Lookup tries the specific scope first.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..config import data_dir

DB_NAME = "answers.db"

SCHEMA = """
create table if not exists answers (
    question_hash text not null,
    scope         text not null default '*',   -- '*' or a portal/domain
    question_text text not null,
    answer        text not null,
    field_type    text,
    times_used    integer not null default 0,
    created_at    text not null,
    last_used_at  text,
    primary key (question_hash, scope)
);
create index if not exists idx_answers_hash on answers(question_hash);
"""

_PUNCT = re.compile(r"[*✱:?!.,;()\[\]]")
_WS = re.compile(r"\s+")
_FILLER = re.compile(r"\b(please|kindly|required|optional)\b", re.I)

# Labels naming half of a split date control — see remember().
_DATE_LABEL = re.compile(r"^(start|end|graduation)\s*(date|month|year)$", re.I)

# UUIDs and long opaque hex/id tokens — machine values, never human answers.
_MACHINE_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    r"|^[0-9a-f]{16,}$",
    re.I,
)


def normalize_question(text: str) -> str:
    """Collapse cosmetic variation so equivalent questions share a key."""
    s = text.lower().strip()
    s = re.sub(r"\(required\)|\(optional\)", " ", s)
    s = _FILLER.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def question_hash(text: str) -> str:
    return hashlib.sha256(normalize_question(text).encode()).hexdigest()[:20]


@dataclass
class CachedAnswer:
    answer: str
    scope: str
    question_text: str
    times_used: int


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(data_dir() / DB_NAME)
    conn.row_factory = sqlite3.Row
    # Two processes touch these databases (a watcher and a fill run);
    # without a busy timeout a moment of contention crashed a watcher
    # mid-tap and her answer was lost with it.
    conn.execute("pragma busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


def lookup(question: str, scope: str = "*") -> CachedAnswer | None:
    """Portal-scoped answer first, then the global one."""
    key = question_hash(question)
    conn = _connect()
    try:
        for candidate_scope in ([scope, "*"] if scope != "*" else ["*"]):
            row = conn.execute(
                "select * from answers where question_hash = ? and scope = ?",
                (key, candidate_scope),
            ).fetchone()
            if row:
                return CachedAnswer(
                    answer=row["answer"],
                    scope=row["scope"],
                    question_text=row["question_text"],
                    times_used=row["times_used"],
                )
        return None
    finally:
        conn.close()


def remember(
    question: str,
    answer: str,
    scope: str = "*",
    field_type: str | None = None,
) -> None:
    # An answer identical to the question carries no information, and for a
    # checkbox it is actively wrong: a lone tick-box's only "option" is its
    # own label, so "Still Student?" was cached as the answer to "Still
    # Student?" and would have ticked it on every future Ashby form —
    # claiming she is a student.
    if normalize_question(answer) == normalize_question(question):
        return

    # Browser artefacts, not answers. "checked" is what read_value reports for
    # a ticked box and "on" is a checkbox's default value attribute; both were
    # cached against real questions, where best_option would later match them
    # onto an arbitrary choice.
    if answer.strip().lower() in {"checked", "on", "true", "false"}:
        return

    # Machine identifiers, not answers. Ashby yes/no cards surfaced their
    # option UUIDs as the option text on one Notion form, and three tapped
    # "answers" arrived here as raw ids ("5ba59b87-…") — poisoning the cache
    # for questions like "Do you have experience with LLMs?". No human
    # answer looks like a UUID or a long opaque hex token.
    if _MACHINE_ID.match(answer.strip()):
        return

    # A date split into month and year selects shares one label, so any cache
    # entry under it holds whichever half was written last — and then answers
    # *both* halves with it. This was guarded in the hand-fill capture, and
    # then resurfaced through engine.learn() promoting rule answers after a
    # fill. Refused here so no write path can recreate it; the education-date
    # rule answers these fields from the resume on every visit anyway.
    if _DATE_LABEL.match(normalize_question(question)):
        return

    key = question_hash(question)
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute(
            """insert into answers (question_hash, scope, question_text, answer,
                                    field_type, times_used, created_at, last_used_at)
               values (?,?,?,?,?,0,?,?)
               on conflict(question_hash, scope) do update set
                 answer = excluded.answer,
                 question_text = excluded.question_text,
                 field_type = coalesce(excluded.field_type, answers.field_type),
                 last_used_at = excluded.last_used_at""",
            (key, scope, question, answer, field_type, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def mark_used(question: str, scope: str = "*") -> None:
    conn = _connect()
    try:
        conn.execute(
            """update answers set times_used = times_used + 1, last_used_at = ?
               where question_hash = ? and scope = ?""",
            (datetime.now().isoformat(timespec="seconds"), question_hash(question),
             scope),
        )
        conn.commit()
    finally:
        conn.close()


def stats() -> dict[str, int]:
    conn = _connect()
    try:
        total = conn.execute("select count(*) n from answers").fetchone()["n"]
        used = conn.execute(
            "select coalesce(sum(times_used),0) n from answers"
        ).fetchone()["n"]
        return {"answers": total, "reuses": used}
    finally:
        conn.close()
