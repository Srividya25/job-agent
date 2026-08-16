"""Confirmation-email watcher.

The submit click is not the outcome. Two "submitted" recordings turned out to
be rejections the page rendered politely, and one real submission (Plaid)
stayed unconfirmable for a day because nobody read the inbox. The employer's
own email is the strongest evidence there is — this module reads it.

Read-only by design: the mailbox is opened with IMAP `readonly=True`, so
nothing is ever marked seen, moved, or deleted. Only headers are fetched for
the broad scan; a body is read only for the handful of messages that mention
a company we actually applied to.

Matching is deliberately conservative. A mail counts only when it BOTH
mentions an applied job's company (in the sender or subject) AND carries
confirmation wording. "Update on your application" without confirmation
wording is surfaced to the user as news, never recorded as a confirmation.
"""

from __future__ import annotations

import email.header
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models import normalize_company

IMAP_HOST = "imap.gmail.com"

# Wording an ATS uses to acknowledge a submission. Kept close to the page
# markers in run.py — the same phrases show up in both places.
_CONFIRM = re.compile(
    r"thank(?:s| you) for (?:applying|your application)"
    r"|application (?:has been |was )?(?:received|submitted)"
    r"|we(?:'ve| have) received your application"
    r"|successfully submitted",
    re.I,
)
# Wording that closes the door. Checked in the body: rejections almost never
# say it in the subject line.
_REJECT = re.compile(
    r"unfortunately|not (?:be )?moving forward|will not be progressing"
    r"|decided to (?:move forward|proceed) with other"
    r"|other candidates|no longer under consideration|not selected",
    re.I,
)
# Anything else that is clearly about an application we sent.
_ABOUT = re.compile(
    r"your application|interview|next steps?|assessment|coding challenge",
    re.I,
)

# normalize_company output still contains words that appear in any inbox;
# matching on them alone would connect "Software Engineering Weekly" to half
# the queue. A company must match on at least one token that is not one of
# these.
_GENERIC_TOKENS = frozenset(
    "software engineering solutions systems data cloud digital global"
    " services security financial health".split()
)


@dataclass
class Mail:
    uid: str
    subject: str
    sender: str  # the full From header


@dataclass
class Finding:
    kind: str  # "confirmed" | "rejected" | "update"
    dedupe_key: str
    company: str
    title: str
    subject: str


# --------------------------------------------------------------------------
# pure matching logic (tested without a mailbox)
# --------------------------------------------------------------------------


def company_tokens(company: str) -> set[str]:
    """The identifying words of a company name."""
    return {
        t for t in normalize_company(company).split()
        if len(t) >= 3 and t not in _GENERIC_TOKENS
    }


def mentions(company: str, subject: str, sender: str) -> bool:
    """Whether a mail plausibly comes from / is about this company.

    A token must appear as a word in the subject or anywhere in the sender —
    sender domains concatenate ("no-reply@greenhouse.plaid.com"), so a
    substring check is right there, while the subject gets word boundaries so
    "Meta" does not match "Metadata".
    """
    tokens = company_tokens(company)
    if not tokens:
        return False
    sender_l, subject_l = sender.lower(), subject.lower()
    for token in tokens:
        if token in sender_l:
            return True
        if re.search(rf"\b{re.escape(token)}\b", subject_l):
            return True
    return False


def classify(subject: str, body: str = "") -> str:
    """"confirmed" / "rejected" / "update" / "" for one mail's text.

    Rejection outranks confirmation: "thank you for applying — unfortunately"
    is one very common email.
    """
    text = f"{subject}\n{body}"
    if _REJECT.search(text):
        return "rejected"
    if _CONFIRM.search(text):
        return "confirmed"
    if _ABOUT.search(text):
        return "update"
    return ""


def match_jobs(mails: list[Mail], jobs: list) -> dict[str, list]:
    """{mail uid: [jobs it mentions]} for the mails that mention any."""
    hits: dict[str, list] = {}
    for mail in mails:
        matched = [
            j for j in jobs if mentions(j.company, mail.subject, mail.sender)
        ]
        if matched:
            hits[mail.uid] = matched
    return hits


# --------------------------------------------------------------------------
# the mailbox
# --------------------------------------------------------------------------


def _decode(raw: str) -> str:
    parts = []
    for value, charset in email.header.decode_header(raw or ""):
        if isinstance(value, bytes):
            parts.append(value.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(value)
    return "".join(parts)


class Mailbox:
    """A read-only IMAP view of the inbox. Never changes anything in it."""

    def __init__(self, address: str, app_password: str, host: str = IMAP_HOST):
        self._imap = imaplib.IMAP4_SSL(host)
        self._imap.login(address, app_password)
        self._imap.select("INBOX", readonly=True)

    def close(self) -> None:
        try:
            self._imap.logout()
        except Exception:  # noqa: BLE001 - closing best-effort
            pass

    def recent(self, days: int) -> list[Mail]:
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        ok, data = self._imap.search(None, f'(SINCE "{since}")')
        if ok != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        mails: list[Mail] = []
        # One fetch for all headers; per-message round trips took minutes on
        # a two-week window.
        ok, chunks = self._imap.fetch(
            b",".join(uids),
            "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])",
        )
        if ok != "OK":
            return []
        uid_iter = iter(uids)
        for chunk in chunks:
            if not isinstance(chunk, tuple) or len(chunk) < 2:
                continue
            msg = email.message_from_bytes(chunk[1])
            mails.append(Mail(
                uid=next(uid_iter, b"").decode(),
                subject=_decode(msg.get("Subject", "")),
                sender=_decode(msg.get("From", "")),
            ))
        return mails

    def body_text(self, uid: str, limit: int = 4000) -> str:
        ok, chunks = self._imap.fetch(uid, "(BODY.PEEK[TEXT])")
        if ok != "OK":
            return ""
        for chunk in chunks:
            if isinstance(chunk, tuple) and len(chunk) >= 2:
                return chunk[1].decode("utf-8", errors="replace")[:limit]
        return ""


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------


def check(days: int = 14, telegram=None, on_event=None) -> list[Finding]:
    """Scan the inbox against every applied-but-unconfirmed job.

    Confirmations are recorded on the job (confirmed_at + the subject line
    as the receipt). Rejections and updates are reported, not recorded —
    deciding what a rejection means for the tracker is the user's call.
    """
    from ..config import load_secrets
    from ..store import db

    secrets = load_secrets()
    if not (secrets.gmail_address and secrets.gmail_app_password):
        raise RuntimeError(
            "GMAIL_ADDRESS / GMAIL_APP_PASSWORD are not in .env — "
            "create an app password at myaccount.google.com/apppasswords"
        )

    with db.connect() as conn:
        jobs = db.applied_unconfirmed(conn)
    if not jobs:
        return []

    box = Mailbox(secrets.gmail_address, secrets.gmail_app_password)
    try:
        mails = box.recent(days)
        if on_event:
            on_event(f"scanned {len(mails)} mail(s) from the last {days} days")

        findings: list[Finding] = []
        seen: set[tuple[str, str]] = set()
        for uid, matched in match_jobs(mails, jobs).items():
            mail = next(m for m in mails if m.uid == uid)
            # Always read the body for a matched mail: "thank you for
            # applying" in the subject with "unfortunately" in the body is a
            # rejection wearing a confirmation's subject line.
            kind = classify(mail.subject, box.body_text(uid))
            if not kind:
                continue
            for job in matched:
                if (job.dedupe_key, kind) in seen:
                    continue
                seen.add((job.dedupe_key, kind))
                findings.append(Finding(
                    kind, job.dedupe_key, job.company, job.title, mail.subject
                ))
    finally:
        box.close()

    confirmed = [f for f in findings if f.kind == "confirmed"]
    with db.connect() as conn:
        for f in confirmed:
            db.set_confirmed(conn, f.dedupe_key, f.subject)

    if telegram and findings:
        telegram.send(format_findings(findings))
    return findings


def format_findings(findings: list[Finding]) -> str:
    icon = {"confirmed": "📬", "rejected": "🕯", "update": "📨"}
    lines = ["Inbox check:"]
    for f in findings:
        lines.append(
            f"{icon[f.kind]} {f.kind}: {f.title} — {f.company}\n"
            f"   “{f.subject[:70]}”"
        )
    return "\n".join(lines)
