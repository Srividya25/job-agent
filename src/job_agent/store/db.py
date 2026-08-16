"""Local SQLite store.

job-agent owns its own state. The Jobtracker Supabase instance is written to
only when an application is actually submitted (Phase 3) — its schema is not
extended, so the open-source tracker repo stays untouched.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from ..config import data_dir
from ..models import ATS, Company, Job, JobStatus, MatchBreakdown, Verdict

DB_NAME = "agent.db"

SCHEMA = """
create table if not exists jobs (
    dedupe_key      text primary key,
    company         text not null,
    title           text not null,
    url             text not null,
    location        text,
    description     text,
    posted_at       text,
    sources         text not null default '[]',
    ats             text not null default 'unknown',
    verdict         text not null default 'ask',
    verdict_reason  text default '',
    match_score     real not null default 0,
    match_breakdown text,
    best_resume     text,
    status          text not null default 'new',
    discovered_at   text not null,
    updated_at      text not null
);
create index if not exists idx_jobs_status on jobs(status);
create index if not exists idx_jobs_score  on jobs(match_score desc);

create table if not exists companies (
    normalized_name text primary key,
    name            text not null,
    ats             text not null default 'unknown',
    ats_slug        text,
    verdict         text not null default 'ask',
    verdict_reason  text default '',
    staffing_conf   real not null default 0,
    h1b_total       integer not null default 0,
    legit_score     real not null default 0.5,
    checked_at      text
);

-- Manual allow/block, always wins over computed verdicts.
create table if not exists company_overrides (
    normalized_name text primary key,
    verdict         text not null,
    note            text,
    created_at      text not null
);

-- Questions the resolver could not answer, sent to the user and awaiting a
-- reply. Ordinal is the number shown in Telegram, so "2 Yes" can be matched
-- back without relying on message threading.
create table if not exists pending_questions (
    id          integer primary key autoincrement,
    dedupe_key  text not null,
    ordinal     integer not null,
    question    text not null,
    field_ref   text,
    field_type  text,
    scope       text not null default '*',
    asked_at    text not null,
    answered_at text,
    answer      text
);
create index if not exists idx_pending_open
    on pending_questions(answered_at) where answered_at is null;

create table if not exists runs (
    id            integer primary key autoincrement,
    started_at    text not null,
    finished_at   text,
    mode          text,
    found         integer default 0,
    new_jobs      integer default 0,
    blocked       integer default 0,
    queued        integer default 0
);

-- One scheduled batch handed to the user for a decision. `mode` is null
-- until she answers auto/manual, which is what keeps a run from acting on
-- its own: no mode, no work.
create table if not exists proposal_runs (
    id          integer primary key autoincrement,
    started_at  text not null,
    label       text not null default '',
    mode        text,
    answered_at text
);

-- One job on that batch. Ordinal is the number shown in Telegram.
create table if not exists proposals (
    id          integer primary key autoincrement,
    run_id      integer not null,
    ordinal     integer not null,
    dedupe_key  text not null,
    match_score real not null default 0,
    tier        text not null default 'ask',
    decision    text,
    decided_at  text,
    acted_at    text
);
create index if not exists idx_proposals_run on proposals(run_id);

-- A filled application shown to the user for approval. values_json is the
-- exact field->value list she saw; nothing is submitted unless a re-fill
-- reproduces it, so "approve" means approving these values and no others.
create table if not exists approvals (
    id           integer primary key autoincrement,
    ordinal      integer not null,
    dedupe_key   text not null,
    values_json  text not null,
    shown_at     text not null,
    decision     text,
    decided_at   text,
    submitted_at text,
    outcome      text
);
create index if not exists idx_approvals_open
    on approvals(decision) where decision is null;
"""


# Columns added after the first release. SQLite has no ADD COLUMN IF NOT
# EXISTS and the schema is replayed on every connect, so they are applied by
# comparing against pragma table_info rather than by running DDL blindly.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    # Asking "End Date" or a question truncated at 90 characters, with no
    # list of the choices the form accepts, is not a question anyone can
    # answer. Both are stored so the message can carry them.
    ("pending_questions", "full_question", "text default ''"),
    ("pending_questions", "options", "text default '[]'"),
    # The employer's acknowledgement email — the only evidence of a
    # submission that outranks the submit endpoint's own response. The
    # subject line is stored as the receipt.
    ("jobs", "confirmed_at", "text"),
    ("jobs", "confirm_subject", "text default ''"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, spec in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"pragma table_info({table})")}
        if column not in existing:
            conn.execute(f"alter table {table} add column {column} {spec}")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(data_dir() / DB_NAME)
    conn.row_factory = sqlite3.Row
    # Two processes touch these databases (a watcher and a fill run);
    # without a busy timeout a moment of contention crashed a watcher
    # mid-tap and her answer was lost with it.
    conn.execute("pragma busy_timeout=5000")
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------


def upsert_job(conn: sqlite3.Connection, job: Job) -> bool:
    """Insert or merge a job. Returns True when newly discovered.

    An existing row keeps its status — a job you already skipped must not
    silently reappear as new on tomorrow's run. Sources are unioned so we can
    see that the same role arrived from Greenhouse and a LinkedIn alert.
    """
    now = datetime.now().isoformat(timespec="seconds")
    existing = conn.execute(
        "select sources, status from jobs where dedupe_key = ?", (job.dedupe_key,)
    ).fetchone()

    if existing:
        merged = sorted(set(json.loads(existing["sources"])) | set(job.sources))
        conn.execute(
            """update jobs set
                 sources = ?, match_score = ?, match_breakdown = ?,
                 best_resume = ?, verdict = ?, verdict_reason = ?,
                 description = coalesce(nullif(?, ''), description),
                 updated_at = ?
               where dedupe_key = ?""",
            (
                json.dumps(merged),
                job.match_score,
                job.match_breakdown.model_dump_json() if job.match_breakdown else None,
                job.best_resume,
                job.verdict.value,
                job.verdict_reason,
                job.description or "",
                now,
                job.dedupe_key,
            ),
        )
        return False

    conn.execute(
        """insert into jobs (
             dedupe_key, company, title, url, location, description, posted_at,
             sources, ats, verdict, verdict_reason, match_score,
             match_breakdown, best_resume, status, discovered_at, updated_at
           ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job.dedupe_key, job.company, job.title, job.url, job.location,
            job.description, job.posted_at.isoformat() if job.posted_at else None,
            json.dumps(sorted(set(job.sources))), job.ats.value,
            job.verdict.value, job.verdict_reason, job.match_score,
            job.match_breakdown.model_dump_json() if job.match_breakdown else None,
            job.best_resume, job.status.value, job.discovered_at.isoformat(), now,
        ),
    )
    return True


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        dedupe_key=row["dedupe_key"],
        company=row["company"],
        title=row["title"],
        url=row["url"],
        location=row["location"],
        description=row["description"],
        posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
        sources=json.loads(row["sources"]),
        ats=ATS(row["ats"]),
        verdict=Verdict(row["verdict"]),
        verdict_reason=row["verdict_reason"] or "",
        match_score=row["match_score"],
        match_breakdown=(
            MatchBreakdown.model_validate_json(row["match_breakdown"])
            if row["match_breakdown"]
            else None
        ),
        best_resume=row["best_resume"],
        status=JobStatus(row["status"]),
        discovered_at=datetime.fromisoformat(row["discovered_at"]).date(),
    )


def list_jobs(
    conn: sqlite3.Connection,
    status: JobStatus | None = None,
    verdict: Verdict | None = None,
    min_score: float = 0.0,
    limit: int = 50,
    max_age_hours: int = 0,
    exclude_proposed: bool = False,
) -> list[Job]:
    clauses = ["match_score >= ?"]
    params: list = [min_score]
    if exclude_proposed:
        # Only jobs never shown in any earlier batch: each run picks up
        # exactly where the last one left off, with no repeats — and a
        # missed run loses nothing, its jobs simply arrive with the next.
        clauses.append(
            "dedupe_key not in (select dedupe_key from proposals)"
        )
    if status:
        clauses.append("status = ?")
        params.append(status.value)
    if verdict:
        clauses.append("verdict = ?")
        params.append(verdict.value)
    if max_age_hours:
        # Applying early measurably matters, so freshness is a hard filter
        # rather than only the 10% recency weight in the score.
        #
        # Postings without a date fall back to when we first saw them: a job
        # discovered on today's run is new *to us* even if the board did not
        # say when it went up. Dropping those would silently lose every
        # posting from a source with no timestamp.
        cutoff = (
            datetime.now().astimezone() - timedelta(hours=max_age_hours)
        ).isoformat()
        clauses.append(
            "(case when posted_at is not null and posted_at != ''"
            "      then posted_at >= ?"
            "      else discovered_at >= ? end)"
        )
        params.extend([cutoff, cutoff[:10]])
    params.append(limit)

    rows = conn.execute(
        f"select * from jobs where {' and '.join(clauses)} "
        "order by match_score desc limit ?",
        params,
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def set_status(conn: sqlite3.Connection, dedupe_key: str, status: JobStatus) -> None:
    conn.execute(
        "update jobs set status = ?, updated_at = ? where dedupe_key = ?",
        (status.value, datetime.now().isoformat(timespec="seconds"), dedupe_key),
    )


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "select status, count(*) n from jobs group by status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# --------------------------------------------------------------------------
# companies
# --------------------------------------------------------------------------


def upsert_company(conn: sqlite3.Connection, company: Company) -> None:
    conn.execute(
        """insert into companies (
             normalized_name, name, ats, ats_slug, verdict, verdict_reason,
             staffing_conf, h1b_total, legit_score, checked_at
           ) values (?,?,?,?,?,?,?,?,?,?)
           on conflict(normalized_name) do update set
             name=excluded.name, ats=excluded.ats, ats_slug=excluded.ats_slug,
             verdict=excluded.verdict, verdict_reason=excluded.verdict_reason,
             staffing_conf=excluded.staffing_conf, h1b_total=excluded.h1b_total,
             legit_score=excluded.legit_score, checked_at=excluded.checked_at""",
        (
            company.normalized_name, company.name, company.ats.value,
            company.ats_slug, company.verdict.value, company.verdict_reason,
            company.staffing.confidence, company.sponsorship.total_filings,
            company.legit_score,
            company.checked_at.isoformat() if company.checked_at else None,
        ),
    )


def load_overrides(conn: sqlite3.Connection) -> dict[str, Verdict]:
    rows = conn.execute(
        "select normalized_name, verdict from company_overrides"
    ).fetchall()
    return {r["normalized_name"]: Verdict(r["verdict"]) for r in rows}


def set_override(
    conn: sqlite3.Connection, normalized_name: str, verdict: Verdict, note: str = ""
) -> None:
    conn.execute(
        """insert into company_overrides (normalized_name, verdict, note, created_at)
           values (?,?,?,?)
           on conflict(normalized_name) do update set
             verdict=excluded.verdict, note=excluded.note""",
        (normalized_name, verdict.value, note,
         datetime.now().isoformat(timespec="seconds")),
    )


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, mode: str = "discover") -> int:
    cur = conn.execute(
        "insert into runs (started_at, mode) values (?,?)",
        (datetime.now().isoformat(timespec="seconds"), mode),
    )
    return int(cur.lastrowid or 0)


def finish_run(conn: sqlite3.Connection, run_id: int, **stats: int) -> None:
    conn.execute(
        """update runs set finished_at = ?, found = ?, new_jobs = ?,
             blocked = ?, queued = ? where id = ?""",
        (
            datetime.now().isoformat(timespec="seconds"),
            stats.get("found", 0), stats.get("new_jobs", 0),
            stats.get("blocked", 0), stats.get("queued", 0), run_id,
        ),
    )


# --------------------------------------------------------------------------
# pending questions
# --------------------------------------------------------------------------


def clear_pending(conn: sqlite3.Connection, dedupe_key: str) -> None:
    conn.execute(
        "delete from pending_questions where dedupe_key = ? and answered_at is null",
        (dedupe_key,),
    )


def add_pending(
    conn: sqlite3.Connection,
    dedupe_key: str,
    ordinal: int,
    question: str,
    field_ref: str = "",
    field_type: str = "",
    scope: str = "*",
    full_question: str = "",
    options: list[str] | None = None,
) -> int:
    """Record a question. `question` is the cache key; `full_question` is what
    the user is actually shown, and `options` are the answers the form takes."""
    cur = conn.execute(
        """insert into pending_questions
             (dedupe_key, ordinal, question, field_ref, field_type, scope,
              asked_at, full_question, options)
           values (?,?,?,?,?,?,?,?,?)""",
        (dedupe_key, ordinal, question, field_ref, field_type, scope,
         datetime.now().isoformat(timespec="seconds"),
         full_question or question, json.dumps(options or [])),
    )
    return int(cur.lastrowid or 0)


def pending_by_id(conn: sqlite3.Connection, pending_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "select * from pending_questions where id = ?", (pending_id,)
    ).fetchone()


def answer_pending(conn: sqlite3.Connection, pending_id: int, answer: str) -> None:
    conn.execute(
        "update pending_questions set answer = ?, answered_at = ? where id = ?",
        (answer, datetime.now().isoformat(timespec="seconds"), pending_id),
    )


def open_questions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "select * from pending_questions where answered_at is null order by ordinal"
    ).fetchall()


# --------------------------------------------------------------------------
# proposals
# --------------------------------------------------------------------------


def latest_proposal_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "select * from proposal_runs order by id desc limit 1"
    ).fetchone()


def last_proposal_started(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("select max(started_at) m from proposal_runs").fetchone()
    try:
        return datetime.fromisoformat(row["m"]) if row and row["m"] else None
    except ValueError:
        return None


def start_proposal(conn: sqlite3.Connection, label: str = "") -> int:
    cur = conn.execute(
        "insert into proposal_runs (started_at, label) values (?,?)",
        (datetime.now().isoformat(timespec="seconds"), label),
    )
    return int(cur.lastrowid or 0)


def add_proposal(
    conn: sqlite3.Connection,
    run_id: int,
    ordinal: int,
    dedupe_key: str,
    match_score: float,
    tier: str,
) -> None:
    conn.execute(
        """insert into proposals (run_id, ordinal, dedupe_key, match_score, tier)
           values (?,?,?,?,?)""",
        (run_id, ordinal, dedupe_key, match_score, tier),
    )


def set_proposal_mode(conn: sqlite3.Connection, run_id: int, mode: str) -> None:
    conn.execute(
        "update proposal_runs set mode = ?, answered_at = ? where id = ?",
        (mode, datetime.now().isoformat(timespec="seconds"), run_id),
    )


def get_proposal_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "select * from proposal_runs where id = ?", (run_id,)
    ).fetchone()


def set_decision(
    conn: sqlite3.Connection, run_id: int, ordinal: int, decision: str
) -> sqlite3.Row | None:
    """Record a per-job decision. Returns the row, or None if no such number."""
    row = conn.execute(
        "select * from proposals where run_id = ? and ordinal = ?",
        (run_id, ordinal),
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "update proposals set decision = ?, decided_at = ? where id = ?",
        (decision, datetime.now().isoformat(timespec="seconds"), row["id"]),
    )
    return conn.execute("select * from proposals where id = ?", (row["id"],)).fetchone()


def decide_remaining(conn: sqlite3.Connection, run_id: int, decision: str) -> int:
    """Apply a decision to every job in the batch still undecided.

    Deliberately not limited to the <70% tier. Once the choice is per
    application, "rest ignore" has to mean the rest — otherwise picking out a
    single job means sending an `ignore` for every other one by hand.
    """
    cur = conn.execute(
        """update proposals set decision = ?, decided_at = ?
           where run_id = ? and decision is null""",
        (decision, datetime.now().isoformat(timespec="seconds"), run_id),
    )
    return cur.rowcount


def proposals_for(
    conn: sqlite3.Connection, run_id: int, decision: str | None = None
) -> list[sqlite3.Row]:
    sql = "select * from proposals where run_id = ?"
    params: list = [run_id]
    if decision is not None:
        sql += " and decision = ?"
        params.append(decision)
    return conn.execute(sql + " order by ordinal", params).fetchall()


def handed_off_unreported(conn: sqlite3.Connection) -> list[tuple[Job, str]]:
    """Jobs she took as hers, with no outcome reported yet. Oldest first.

    "Hers" = decided manual on some batch, or Workday tier ("yours"). No
    outcome = the job still sits in status NEW: an Applied/Already
    applied/Ignored tap would have moved it to applied or skipped. Returns
    (job, iso timestamp of when it became hers).
    """
    rows = conn.execute(
        """select p.dedupe_key,
                  max(coalesce(p.decided_at, r.started_at)) as handed_at
             from proposals p join proposal_runs r on r.id = p.run_id
            where p.decision = 'manual' or p.tier = 'yours'
            group by p.dedupe_key"""
    ).fetchall()
    out = []
    for row in rows:
        job = job_by_key(conn, row["dedupe_key"])
        if job is not None and job.status is JobStatus.NEW:
            out.append((job, row["handed_at"] or ""))
    out.sort(key=lambda pair: pair[1])
    return out


def mark_acted(conn: sqlite3.Connection, proposal_id: int) -> None:
    conn.execute(
        "update proposals set acted_at = ? where id = ?",
        (datetime.now().isoformat(timespec="seconds"), proposal_id),
    )


# --------------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------------


def next_approval_ordinal(conn: sqlite3.Connection) -> int:
    """Numbers keep climbing across runs.

    Reusing "1" every batch would let yesterday's `submit 1`, sent late,
    submit an entirely different job today.
    """
    row = conn.execute("select max(ordinal) m from approvals").fetchone()
    return int((row["m"] or 0) + 1)


def add_approval(
    conn: sqlite3.Connection, ordinal: int, dedupe_key: str, values_json: str
) -> int:
    cur = conn.execute(
        """insert into approvals (ordinal, dedupe_key, values_json, shown_at)
           values (?,?,?,?)""",
        (ordinal, dedupe_key, values_json,
         datetime.now().isoformat(timespec="seconds")),
    )
    return int(cur.lastrowid or 0)


def open_approvals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "select * from approvals where decision is null order by ordinal"
    ).fetchall()


def approval_by_ordinal(conn: sqlite3.Connection, ordinal: int) -> sqlite3.Row | None:
    return conn.execute(
        """select * from approvals where ordinal = ? and decision is null
           order by shown_at limit 1""",
        (ordinal,),
    ).fetchone()


def set_approval(
    conn: sqlite3.Connection, approval_id: int, decision: str
) -> None:
    conn.execute(
        "update approvals set decision = ?, decided_at = ? where id = ?",
        (decision, datetime.now().isoformat(timespec="seconds"), approval_id),
    )


def reopen_approval(
    conn: sqlite3.Connection, approval_id: int, values_json: str
) -> None:
    """Put an approval back in front of the user with an updated snapshot.

    Used when the submit-time refill grew *new* fields the review never
    showed: the values she approved are unchanged, but the form now says
    more, so her `submit` is asked for again against the fuller snapshot.
    """
    conn.execute(
        "update approvals set decision = null, decided_at = null, "
        "values_json = ? where id = ?",
        (values_json, approval_id),
    )


def record_submission(
    conn: sqlite3.Connection, approval_id: int, outcome: str
) -> None:
    conn.execute(
        "update approvals set submitted_at = ?, outcome = ? where id = ?",
        (datetime.now().isoformat(timespec="seconds"), outcome, approval_id),
    )


def applied_unconfirmed(conn: sqlite3.Connection) -> list[Job]:
    """Applied jobs with no confirmation email recorded yet."""
    rows = conn.execute(
        "select * from jobs where status = ? and confirmed_at is null",
        (JobStatus.APPLIED.value,),
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def set_confirmed(conn: sqlite3.Connection, dedupe_key: str, subject: str) -> None:
    conn.execute(
        "update jobs set confirmed_at = ?, confirm_subject = ? where dedupe_key = ?",
        (datetime.now().isoformat(timespec="seconds"), subject[:200], dedupe_key),
    )


def job_by_key(conn: sqlite3.Connection, dedupe_key: str) -> Job | None:
    row = conn.execute(
        "select * from jobs where dedupe_key = ?", (dedupe_key,)
    ).fetchone()
    return _row_to_job(row) if row else None


def resolve_pending(
    conn: sqlite3.Connection, ordinal: int, answer: str
) -> sqlite3.Row | None:
    """Record an answer against the oldest open question with this number."""
    row = conn.execute(
        """select * from pending_questions
           where ordinal = ? and answered_at is null
           order by asked_at limit 1""",
        (ordinal,),
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "update pending_questions set answer = ?, answered_at = ? where id = ?",
        (answer, datetime.now().isoformat(timespec="seconds"), row["id"]),
    )
    return row
