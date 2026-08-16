"""Telegram as the control channel.

The agent applies in the background and reaches the user only when it needs an
answer. Telegram is used rather than a terminal prompt because a prompt only
works if someone is watching the terminal — the whole point is that they are
not.

Nothing here ever blocks waiting for a reply. Questions are sent numbered, the
job is parked, and answers are collected at the start of the next run.

REST directly via httpx, same style as tracker.py — no SDK for two endpoints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from ..config import load_secrets

API = "https://api.telegram.org"
TIMEOUT = 20.0


@dataclass
class Reply:
    """One parsed answer: "2 Yes" -> ordinal 2, answer "Yes"."""

    ordinal: int
    answer: str
    update_id: int
    raw: str


# "2 Yes", "2. Yes", "2) yes please", "#2 - Yes"
_REPLY = re.compile(r"^\s*#?(\d{1,3})\s*[.):\-]?\s+(.{1,500})$", re.S)


# Greetings and status pokes, never answers to an application question.
_CHATTER = re.compile(
    # Longest alternatives first: regex alternation takes the first match, so
    # a bare "hey" would otherwise win against "hey what's up" and then fail
    # the end-anchor, letting chatter through as an answer.
    r"^(?:"
    r"hey +what['\u2019]?s +up|what['\u2019]?s +up|how['\u2019]?s +it +going|"
    r"thank ?you|thanks|hello|okay|done|sent|status|help|hey|hi|yo|ok|ty"
    r")[\s.!?]*$",
    re.I,
)

_LINE = re.compile(r"^\s*#?(\d{1,3})\s*[.):\-]?\s+(.+?)\s*$")


def parse_replies(text: str, update_id: int = 0) -> list[Reply]:
    """Parse one message into every answer it contains.

    People naturally answer several questions in one message:

        1. no
        2. yes

    Treating that as a single answer put "no\\n2. yes" against question 1 and
    silently lost question 2 — wrong data on a real application.

    The rule: if two or more lines start with an ordinal, each is its own
    answer. If only one does, the whole remainder is a single multi-line
    answer, which is what a long free-text response looks like.
    """
    if not text or not text.strip():
        return []

    lines = text.strip().splitlines()
    numbered = [(i, m) for i, line in enumerate(lines) if (m := _LINE.match(line))]

    if len(numbered) >= 2:
        return [
            Reply(
                ordinal=int(m.group(1)),
                answer=m.group(2).strip(),
                update_id=update_id,
                raw=lines[i],
            )
            for i, m in numbered
            if m.group(2).strip()
        ]

    if single := parse_reply(text, update_id):
        return [single]

    # No ordinal. Kept with ordinal 0 so the caller may attach it when exactly
    # one question is outstanding. Anything that reads as conversation rather
    # than an answer is dropped — silently answering a real application with
    # "hey what's up" is far worse than ignoring a message.
    body = text.strip()
    if not body or len(body) > 500 or body.startswith("/"):
        return []
    if body.endswith("?"):
        return []                      # a question, not an answer
    if _CHATTER.match(body):
        return []
    return [Reply(ordinal=0, answer=body, update_id=update_id, raw=body)]


def parse_reply(text: str, update_id: int = 0) -> Reply | None:
    """Parse a single "<n> <answer>", allowing a multi-line answer body.

    Deliberately not using Telegram's reply-to-message threading: that breaks
    the moment the user types a fresh message instead of swiping to reply,
    which is most of the time on mobile.
    """
    if not text:
        return None
    match = _REPLY.match(text.strip())
    if not match:
        return None
    answer = match.group(2).strip()
    if not answer:
        return None
    return Reply(
        ordinal=int(match.group(1)),
        answer=answer,
        update_id=update_id,
        raw=text.strip(),
    )


class Telegram:
    def __init__(
        self, token: str, chat_id: str, jobs_chat_id: str | None = None
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        # The job-postings channel. Defaults to the main chat, so one-chat
        # setups behave exactly as before; set TELEGRAM_JOBS_CHAT_ID to split
        # the ranked lists away from questions and reviews.
        self.jobs_chat_id = jobs_chat_id or chat_id

    @property
    def allowed_chats(self) -> set[str]:
        """Chats whose messages and taps this bot listens to."""
        return {str(self.chat_id), str(self.jobs_chat_id)}

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> Telegram | None:
        """None when unconfigured, so callers degrade to terminal output."""
        s = load_secrets()
        if not s.telegram_bot_token or not s.telegram_chat_id:
            return None
        return cls(s.telegram_bot_token, s.telegram_chat_id, s.telegram_jobs_chat_id)

    @staticmethod
    def configured() -> bool:
        s = load_secrets()
        return bool(s.telegram_bot_token and s.telegram_chat_id)

    def _url(self, method: str) -> str:
        return f"{API}/bot{self.token}/{method}"

    # ------------------------------------------------------------------
    def send(
        self,
        text: str,
        buttons: list[list[tuple[str, str]]] | None = None,
        chat_id: str | None = None,
    ) -> bool:
        """Send a message, optionally with tappable buttons.

        `buttons` is rows of (label, callback_data). Typing `4 auto` on a
        phone for every job is the kind of friction that stops a system being
        used at all, and Telegram will render the choice as buttons for free.
        `chat_id` routes to a specific chat; default is the main one.
        """
        payload: dict = {
            "chat_id": chat_id or self.chat_id,
            "text": text[:4000],  # Telegram caps messages at 4096
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row]
                    for row in buttons
                ]
            }
        try:
            r = httpx.post(self._url("sendMessage"), json=payload, timeout=TIMEOUT)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        """Clear the spinner on a tapped button. Without this it spins on."""
        try:
            r = httpx.post(
                self._url("answerCallbackQuery"),
                json={"callback_query_id": callback_id, "text": text[:200]},
                timeout=TIMEOUT,
            )
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def send_photo(
        self, png: bytes, caption: str = "", chat_id: str | None = None
    ) -> bool:
        """A screenshot of a stuck form is worth more than describing it."""
        try:
            r = httpx.post(
                self._url("sendPhoto"),
                data={"chat_id": chat_id or self.chat_id, "caption": caption[:1000]},
                files={"photo": ("form.png", png, "image/png")},
                timeout=60.0,
            )
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def download_file(self, file_id: str, max_bytes: int = 15_000_000) -> bytes | None:
        """Fetch a file the user sent to the bot (e.g. a resume PDF)."""
        try:
            r = httpx.get(
                self._url("getFile"), params={"file_id": file_id}, timeout=TIMEOUT
            )
            if r.status_code != 200:
                return None
            path = (r.json().get("result") or {}).get("file_path")
            if not path:
                return None
            f = httpx.get(
                f"{API}/file/bot{self.token}/{path}", timeout=120.0
            )
            if f.status_code != 200 or len(f.content) > max_bytes:
                return None
            return f.content
        except httpx.HTTPError:
            return None

    # ------------------------------------------------------------------
    def poll(self, offset: int = 0) -> tuple[list[Reply], int]:
        """Fetch replies since `offset`. Returns (replies, next_offset).

        Non-blocking: timeout=0 so a run never stalls on this.
        """
        try:
            r = httpx.get(
                self._url("getUpdates"),
                params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'},
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                return [], offset
            payload = r.json()
        except (httpx.HTTPError, ValueError):
            return [], offset

        replies: list[Reply] = []
        next_offset = offset
        for update in payload.get("result", []):
            update_id = update.get("update_id", 0)
            next_offset = max(next_offset, update_id + 1)
            message = update.get("message") or {}
            if str(message.get("chat", {}).get("id")) not in self.allowed_chats:
                continue  # not our conversation
            replies.extend(parse_replies(message.get("text") or "", update_id))
        return replies, next_offset

    def discover_chat_id(self) -> str | None:
        """Chat id of whoever last messaged the bot — used by `telegram setup`."""
        try:
            r = httpx.get(self._url("getUpdates"), params={"timeout": 0}, timeout=TIMEOUT)
            if r.status_code != 200:
                return None
            for update in reversed(r.json().get("result", [])):
                chat = (update.get("message") or {}).get("chat") or {}
                if chat.get("id"):
                    return str(chat["id"])
        except (httpx.HTTPError, ValueError):
            return None
        return None


# ----------------------------------------------------------------------
# message formatting
# ----------------------------------------------------------------------


def format_questions(
    company: str, title: str, questions: list[tuple[int, str]], url: str = ""
) -> str:
    """Numbered so replies can be matched without threading."""
    lines = [f"🟡 Stuck: {title} — {company}", ""]
    for ordinal, question in questions:
        lines.append(f"{ordinal}. {question}")
    lines += ["", "Reply like:  2 Yes"]
    if url:
        lines += ["", url]
    return "\n".join(lines)


# What kind of answer each control actually takes, so a free-text question
# says so rather than leaving the user guessing the format.
_ANSWER_HINT = {
    "date": "a date, e.g. 2024-05",
    "url": "a link",
    "email": "an email address",
    "tel": "a phone number",
    "number": "a number",
    "checkbox": "Yes or No",
    "textarea": "free text",
}


def format_question(
    ordinal: int,
    question: str,
    field_type: str = "text",
    options: list[str] | None = None,
    company: str = "",
    title: str = "",
) -> str:
    """One question, with everything needed to answer it.

    Previously each was a bare label: "End Date", or a question cut off at 90
    characters, or a multiple-choice field with its choices omitted. The
    options matter most — the form only accepts its own wording, so guessing
    at it fails silently.
    """
    header = f"❓ Question {ordinal}"
    if company:
        header += f" · {title} — {company}" if title else f" · {company}"

    lines = [header, "", question]
    if options:
        lines += ["", "Choices:"]
        lines += [f"  • {option}" for option in options]
        lines += ["", f"Reply `{ordinal} <your choice>` or tap below."]
    else:
        hint = _ANSWER_HINT.get(field_type, "")
        lines += ["", f"Reply `{ordinal} <answer>`" + (f" — {hint}" if hint else "")]
    return "\n".join(lines)


MULTI_SEP = " | "


def question_buttons(
    pending_id: int, options: list[str], limit: int = 8, multi: bool = False,
    chosen: list[str] | None = None,
) -> list[list[tuple[str, str]]]:
    """A button per choice, two to a row.

    Only the option's index travels in callback_data — Telegram caps it at 64
    bytes and real option text ("I identify as one or more of the
    classifications of a protected veteran") blows straight past that.

    `multi` is for "select all that apply": taps accumulate instead of
    replacing, already-chosen options are ticked, and a Done button closes the
    question. Without it a multi-select question recorded only the last tap,
    so "Why are you interested in working at Plaid?" went out with one of five
    answers.
    """
    chosen = chosen or []
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for index, option in enumerate(options[:limit]):
        mark = "☑️ " if multi and option in chosen else ("☐ " if multi else "")
        row.append((f"{mark}{option[:26]}", f"q:{pending_id}:{index}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if multi:
        rows.append([("✅ Done", f"q:{pending_id}:done")])
    return rows


def parse_question_callback(data: str) -> tuple[int, int | str] | None:
    """"q:17:2" -> (17, 2). "q:17:done" -> (17, "done")."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "q":
        return None
    try:
        pending_id = int(parts[1])
    except ValueError:
        return None
    if parts[2] == "done":
        return pending_id, "done"
    try:
        return pending_id, int(parts[2])
    except ValueError:
        return None


def is_multi_select(field_type: str, options: list[str], question: str = "") -> bool:
    """Whether this question takes more than one answer.

    A checkbox group is the giveaway: radios and selects are one-of. The
    wording is checked too, because some forms use "select all that apply"
    above controls that are not marked up as a group.
    """
    if len(options) < 2:
        return False
    if "select all that apply" in (question or "").lower():
        return True
    # A bare Yes/No pair is one-of regardless of the control that carries it.
    # Ashby's yes/no widgets are checkbox-typed state carriers, and treating
    # them as multi-select sent her Yes taps into a toggle flow where the
    # second tap silently UN-selected her answer.
    if {o.strip().lower() for o in options} == {"yes", "no"}:
        return False
    return field_type == "checkbox"


def format_applied(company: str, title: str, score: float, resume: str) -> str:
    return (
        f"✅ Applied: {title} — {company}\n"
        f"match {score:.0%} · resume: {resume}"
    )


def format_summary(applied: int, stuck: int, skipped: int) -> str:
    parts = [f"✅ {applied} applied"]
    if stuck:
        parts.append(f"🟡 {stuck} need answers")
    if skipped:
        parts.append(f"⏭ {skipped} skipped")
    return " · ".join(parts)
