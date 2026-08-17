"""Proposal tiering, decision parsing, and the guard against misrouting.

No network: all pure functions over hand-built jobs.
"""

from __future__ import annotations

import pytest

from job_agent import propose
from job_agent.models import ATS, Job, JobStatus
from job_agent.propose import Decision, Mode


def make_job(score: float, company: str = "Acme", title: str = "Software Engineer") -> Job:
    return Job(
        dedupe_key=f"{company}-{title}-{score}",
        company=company,
        title=title,
        url=f"https://example.com/{score}",
        location="San Jose, CA",
        ats=ATS.GREENHOUSE,
        match_score=score,
        best_resume="general",
        status=JobStatus.NEW,
    )


# --------------------------------------------------------------------------
# tiering
# --------------------------------------------------------------------------


def test_no_score_fills_by_default() -> None:
    """Her 2026-08-16 change: she decides every job, whatever it scores.

    Until then >=70% filled on the batch-level `auto` alone; now even a 99%
    match waits for an explicit per-job auto.
    """
    items = propose.build([make_job(0.99), make_job(0.70), make_job(0.4)])
    assert [i.tier for i in items] == ["ask", "ask", "ask"]


def test_format_entries_keeps_every_job_whole() -> None:
    """The buttonless tail of a big batch: all jobs, chunked, none split."""
    items = propose.build([make_job(0.5 + i / 1000, f"Company{i}") for i in range(60)])
    messages = propose.format_entries(items)
    assert len(messages) > 1  # 60 entries cannot fit one Telegram message
    joined = "\n".join(messages)
    for i in range(60):
        assert f"Company{i} " in joined
    assert all(len(m) <= propose.TELEGRAM_LIMIT for m in messages)


def test_posting_age_is_always_visible() -> None:
    """An old posting that surfaced late must say so — freshness is judged
    by her, never disguised."""
    from datetime import datetime, timedelta

    fresh = make_job(0.9, "FreshCo")
    fresh.posted_at = datetime.now()
    old = make_job(0.8, "OldCo")
    old.posted_at = datetime.now() - timedelta(days=30)
    unknown = make_job(0.7, "MysteryCo")

    assert "today" in propose.age_label(fresh)
    assert "⏳" in propose.age_label(old)
    assert "unknown" in propose.age_label(unknown)

    items = propose.build([fresh, old])
    assert "🆕 posted today" in propose.job_button_text(items[0])
    assert "⏳" in propose.job_button_text(items[1])
    joined = "\n".join(propose.format_proposal(items, "10 am", 2))
    assert "🆕 posted today" in joined


def test_every_job_carries_the_decision_hint() -> None:
    messages = propose.format_proposal(
        propose.build([make_job(0.95), make_job(0.5)]), "10 am", 2
    )
    joined = "\n".join(messages)
    assert "`1 auto`" in joined
    assert "`2 auto`" in joined


def test_every_job_is_listed_regardless_of_score() -> None:
    """She asked to see everything, not only what clears a threshold."""
    items = propose.build([make_job(0.9), make_job(0.4), make_job(0.05)])
    assert len(items) == 3


def test_ranked_and_numbered_from_one() -> None:
    items = propose.build([make_job(0.3), make_job(0.9), make_job(0.6)])
    assert [i.ordinal for i in items] == [1, 2, 3]
    assert [i.percent for i in items] == [90, 60, 30]


# --------------------------------------------------------------------------
# workday — hers to apply, never the agent's
# --------------------------------------------------------------------------


def test_workday_ats_is_tier_yours_regardless_of_score() -> None:
    """Her decision: the agent never creates or holds Workday accounts."""
    job = make_job(0.95)
    job.ats = ATS.WORKDAY
    (item,) = propose.build([job])
    assert item.tier == "yours"


def test_workday_by_url_alone_is_tier_yours() -> None:
    """Aggregators deliver Workday URLs with ats UNKNOWN; the URL decides."""
    job = make_job(0.95)
    job.ats = ATS.UNKNOWN
    job.url = "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/x"
    (item,) = propose.build([job])
    assert item.tier == "yours"


def test_workday_jobs_are_listed_with_their_url() -> None:
    """She applies by hand, so the message must carry the link."""
    job = make_job(0.95)
    job.ats = ATS.WORKDAY
    messages = propose.format_proposal(propose.build([job]), "10 am", 1)
    joined = "\n".join(messages)
    assert "Workday" in joined
    assert job.url in joined
    # No decision hint: there is no decision to make on a Workday job.
    assert "1 auto" not in joined


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("auto", Mode.AUTO), ("manual", Mode.MANUAL), ("mode auto", Mode.AUTO),
     ("  MANUAL  ", Mode.MANUAL), ("automatic", None), ("6 auto", None)],
)
def test_parse_mode(text: str, expected: Mode | None) -> None:
    assert propose.parse_mode(text) == expected


@pytest.mark.parametrize(
    ("text", "ordinal", "decision"),
    [
        ("6 ignore", 6, Decision.IGNORE),
        ("6. auto", 6, Decision.AUTO),
        ("#12 manual", 12, Decision.MANUAL),
        ("12 skip", 12, Decision.IGNORE),
        ("3) AUTO", 3, Decision.AUTO),
    ],
)
def test_parse_decision(text: str, ordinal: int, decision: Decision) -> None:
    assert propose.parse_decision(text) == (ordinal, decision)


def test_bulk_shortcut() -> None:
    assert propose.parse_bulk("rest ignore") is Decision.IGNORE
    assert propose.parse_bulk("rest auto") is Decision.AUTO
    assert propose.parse_bulk("ignore") is None


# --------------------------------------------------------------------------
# the misrouting guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["auto", "manual", "6 ignore", "rest ignore", "12 skip"])
def test_decision_replies_are_owned(text: str) -> None:
    assert propose.is_decision_reply(text)


@pytest.mark.parametrize(
    "text",
    [
        "2 Yes",                              # a real form answer
        "1 I have not worked there before",
        "Master of Science in Software Engineering",
        "120000",
        "hello",
    ],
)
def test_form_answers_are_not_swallowed(text: str) -> None:
    """A form answer must still reach run.consume_replies.

    If this guard were too greedy, "2 Yes" would be eaten by the proposal
    watcher and the question it answered would stay open forever.
    """
    assert not propose.is_decision_reply(text)


def test_answer_containing_the_word_auto_is_not_a_decision() -> None:
    """"1 automation testing" is an answer, not a decision on job 1."""
    assert not propose.is_decision_reply("1 automation testing")
    assert propose.parse_decision("1 automation testing") is None


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def test_messages_stay_under_the_telegram_cap() -> None:
    """Telegram truncates at 4000; a cut-off message loses a URL."""
    items = propose.build([make_job(0.5 + i / 1000, f"Company{i}") for i in range(120)])
    messages = propose.format_proposal(items, "10:00 am", 610)
    assert messages
    assert all(len(m) <= propose.TELEGRAM_LIMIT for m in messages)


def test_no_job_is_split_across_messages() -> None:
    items = propose.build([make_job(0.5 + i / 1000, f"Company{i}") for i in range(120)])
    messages = propose.format_proposal(items, "10:00 am", 610)
    joined = "\n\n".join(messages)
    for item in items:
        assert item.job.url in joined


def test_percentage_and_url_are_shown() -> None:
    items = propose.build([make_job(0.83, "Stripe")])
    body = "\n".join(propose.format_proposal(items, "10:00 am", 1))
    assert "83%" in body
    assert "https://example.com/0.83" in body


def test_sub_seventy_entries_carry_the_reply_hint() -> None:
    items = propose.build([make_job(0.65)])
    body = "\n".join(propose.format_proposal(items, "10:00 am", 1))
    assert "1 auto" in body and "1 manual" in body and "1 ignore" in body


def test_empty_batch_says_so() -> None:
    body = "\n".join(propose.format_proposal([], "10:00 am", 0))
    assert "Nothing queued" in body


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("auto apply", Mode.AUTO),
        ("manual apply", Mode.MANUAL),
        ("auto-apply", Mode.AUTO),
        ("Auto Apply", Mode.AUTO),
        ("auto applying", Mode.AUTO),
        # Still not a mode: these must fall through to the nudge.
        ("apply", None),
        ("yes", None),
        ("approved", None),
    ],
)
def test_mode_accepts_the_phrasing_people_actually_use(
    text: str, expected: Mode | None
) -> None:
    """A real reply was lost because only the bare word parsed.

    Telegram had already discarded it by the time the batch reported no mode,
    so the batch sat waiting on an answer that had been given.
    """
    assert propose.parse_mode(text) == expected


# --------------------------------------------------------------------------
# per-application choice
# --------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from job_agent import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from job_agent.store import db

    return db


def test_only_explicit_auto_fills(isolated_db) -> None:
    """Nothing fills by default — a job fills only on her per-job `auto`.

    Score is irrelevant: an undecided 79% stays untouched, and a 65% she
    marked auto fills. (Before 2026-08-16 the >=70% tier filled on the
    batch-level go signal alone; she asked for every job to wait for her.)
    """
    from job_agent import schedule
    from job_agent.propose import Mode

    db = isolated_db
    jobs = [
        make_job(0.79, "Stripe"), make_job(0.76, "Salesforce"),
        make_job(0.75, "MongoDB"), make_job(0.65, "Chime"),
        make_job(0.40, "Adobe"),
    ]
    with db.connect() as conn:
        for job in jobs:
            db.upsert_job(conn, job)
    run_id, _ = schedule.open_batch(jobs, "10:00 am", 5, telegram=None)

    with db.connect() as conn:
        db.set_decision(conn, run_id, 2, "manual")   # hers by hand
        db.set_decision(conn, run_id, 3, "ignore")   # dropped
        db.set_decision(conn, run_id, 4, "auto")     # a <70% she chose

    # Stripe (79%) and Adobe (40%) were never decided — both stay untouched.
    filled = [k.split("-")[0] for _, k in schedule.to_fill(run_id, Mode.AUTO)]
    assert filled == ["Chime"]

    # Auto taps fill immediately and are marked acted; the end-of-batch
    # sweep must not fill the same application twice.
    with db.connect() as conn:
        for pid, _ in schedule.to_fill(run_id, Mode.AUTO):
            db.mark_acted(conn, pid)
    assert schedule.to_fill(run_id, Mode.AUTO) == []


def test_batch_window_measures_discovery_not_posting_date(isolated_db) -> None:
    """A July posting that surfaces in August must not be invisible.

    Boards list postings late; Adobe (posted 7/24, discovered 8/17) was
    silently dropped by a posted-date window. The batch filters by when a
    job ENTERED THE SYSTEM, so nothing late-surfacing is ever lost.
    """
    from datetime import datetime

    db = isolated_db
    job = make_job(0.8, "Adobe")
    job.posted_at = datetime(2026, 7, 24)      # posted long ago
    with db.connect() as conn:
        db.upsert_job(conn, job)               # discovered today

    with db.connect() as conn:
        by_posted = db.list_jobs(conn, status=JobStatus.NEW, min_score=0.0,
                                 max_age_hours=48)
        by_discovered = db.list_jobs(conn, status=JobStatus.NEW, min_score=0.0,
                                     max_age_hours=48, age_by="discovered")
    assert by_posted == []                      # the leak
    assert [j.company for j in by_discovered] == ["Adobe"]


def test_a_proposed_job_is_never_proposed_again(isolated_db) -> None:
    """Each batch picks up where the last left off — no repeats.

    The 10am run shows a job; the 3pm run must not show it again, decided or
    not. A job missed because a run never happened is still unproposed and
    arrives with the next batch, so nothing is lost to a closed laptop.
    """
    from job_agent import schedule

    db = isolated_db
    first, second = make_job(0.9, "Stripe"), make_job(0.8, "Chime")
    with db.connect() as conn:
        db.upsert_job(conn, first)
    schedule.open_batch([first], "10:00 am", 1, telegram=None)

    with db.connect() as conn:
        db.upsert_job(conn, second)  # discovered between the runs
        fresh = db.list_jobs(
            conn, status=JobStatus.NEW, min_score=0.0, exclude_proposed=True
        )
    assert [j.company for j in fresh] == ["Chime"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("window 3d", "3d"), ("since 24h", "24h"), ("WINDOW 1w", "1w"),
     ("window all", "all"), ("windows update", None), ("3d", None)],
)
def test_parse_since_command(text: str, expected: str | None) -> None:
    assert propose.parse_since_command(text) == expected


def test_since_command_is_never_a_form_answer() -> None:
    """Typed at the wrong moment, "window 3d" must not be cached as the
    answer to an open application question."""
    assert propose.is_decision_reply("window 3d")


def test_window_set_from_telegram_persists(isolated_db) -> None:
    from job_agent import schedule

    class T:
        sent: list[str] = []

        def send(self, text, buttons=None, chat_id=None):
            self.sent.append(text)

    t = T()
    assert schedule.apply_since_command(t, "3d")
    assert schedule.load_window() == "3d"
    assert schedule.apply_since_command(t, "all")
    assert schedule.load_window() == ""
    # Garbage is rejected with a message, and the setting is untouched.
    schedule.apply_since_command(t, "3d")
    assert not schedule.apply_since_command(t, "fortnightly")
    assert schedule.load_window() == "3d"
    assert any("⚠️" in m for m in t.sent)


@pytest.mark.parametrize(("text", "expected"), [
    ("done", True),
    ("done submitted stripe", True),        # the real message that was ignored
    ("im done", True),
    ("finished!", True),
    ("ok submitted", True),
    ("not done yet", False),
    ("almost done", False),
    ("how do I know it's done?", False),
    ("2 Yes", False),
    ("", False),
])
def test_done_signal_understands_her_words(text: str, expected: bool) -> None:
    from job_agent.schedule import is_done_signal

    assert is_done_signal(text) is expected


def test_missed_slot_detection() -> None:
    """Powered-off machines skip launchd calendar jobs; catch-up owes the
    latest uncovered slot and nothing when the last batch covered it."""
    from datetime import datetime

    from job_agent.schedule import missed_slot

    now = datetime(2026, 8, 16, 13, 9)
    # Last batch ran at 10:02 today — the 10:00 slot is covered.
    assert missed_slot(datetime(2026, 8, 16, 10, 2), now) is None
    # Laptop was off since yesterday evening: owes today's 10:00.
    assert missed_slot(datetime(2026, 8, 15, 15, 1), now) == \
        datetime(2026, 8, 16, 10, 0)
    # After 15:00 with only the morning run done: owes 15:00.
    assert missed_slot(datetime(2026, 8, 16, 10, 2),
                       datetime(2026, 8, 16, 17, 0)) == \
        datetime(2026, 8, 16, 15, 0)
    # Never ran at all: owes the latest past slot.
    assert missed_slot(None, now) == datetime(2026, 8, 16, 10, 0)


def test_skill_gaps_aggregate_and_rank() -> None:
    from job_agent.models import MatchBreakdown

    jobs = []
    for i, missing in enumerate(
        [["Kubernetes", "Go"], ["Kubernetes"], ["Kubernetes", "Go"], ["Rust"]]
    ):
        job = make_job(0.6, f"Co{i}")
        # The posting must actually ask for the skill, or it is not a
        # resume gap — a job that never says Kubernetes proves nothing.
        job.description = "We use " + " and ".join(missing)
        job.match_breakdown = MatchBreakdown(missing_skills=missing)
        jobs.append(job)
    unmentioned = make_job(0.6, "CoX")
    unmentioned.description = "A generalist role."
    unmentioned.match_breakdown = MatchBreakdown(missing_skills=["Kubernetes"])
    jobs.append(unmentioned)
    gaps = propose.skill_gaps(propose.build(jobs))
    assert gaps[0] == ("Kubernetes", 3)
    assert gaps[1] == ("Go", 2)
    # A one-off gap is noise, not a pattern.
    assert all(skill != "Rust" for skill, _ in gaps)

    text = propose.format_skill_gaps(gaps, 5)
    assert "Kubernetes — 3 of 5 jobs" in text


def test_no_gaps_no_message() -> None:
    items = propose.build([make_job(0.9)])
    assert propose.skill_gaps(items) == []


def test_a_new_review_supersedes_the_old_one(isolated_db) -> None:
    """A refill's fresh snapshot replaces the stale open review — three
    duplicate Stripe reviews taught this."""
    db = isolated_db
    with db.connect() as conn:
        db.add_approval(conn, 1, "key-x", "[]")
        db.add_approval(conn, 2, "key-x", "[]")
        open_now = db.open_approvals(conn)
    assert [r["ordinal"] for r in open_now] == [2]


def test_answered_auto_jobs_are_ready_for_refill(isolated_db) -> None:
    """Her flow: stuck -> she answers -> the review comes back NOW."""
    from job_agent import schedule
    from job_agent.listen import _ready_for_refill

    db = isolated_db
    job = make_job(0.8, "Stripe")
    with db.connect() as conn:
        db.upsert_job(conn, job)
    run_id, _ = schedule.open_batch([job], "10:00 am", 1, telegram=None)
    with db.connect() as conn:
        db.set_decision(conn, run_id, 1, "auto")
        qid = db.add_pending(conn, job.dedupe_key, 1, "Visa type?", None, "text", "*")

    with db.connect() as conn:
        assert _ready_for_refill(conn) == []      # question still open
        db.answer_pending(conn, qid, "F-1 OPT")
        db.set_status(conn, job.dedupe_key, JobStatus.NEW)
    with db.connect() as conn:
        ready = _ready_for_refill(conn)
    assert [k for _, k in ready] == [job.dedupe_key]

    with db.connect() as conn:                    # refill moved it out of NEW
        db.set_status(conn, job.dedupe_key, JobStatus.PENDING_APPROVAL)
        assert _ready_for_refill(conn) == []


def test_handed_off_jobs_are_listed_until_reported(isolated_db) -> None:
    """Manual picks and Workday jobs wait on her outcome; a tap clears them."""
    from job_agent import schedule

    db = isolated_db
    manual_job = make_job(0.8, "Stripe")
    workday_job = make_job(0.7, "Nvidia")
    workday_job.ats = ATS.WORKDAY
    undecided = make_job(0.6, "Chime")
    with db.connect() as conn:
        for j in (manual_job, workday_job, undecided):
            db.upsert_job(conn, j)
    run_id, _ = schedule.open_batch(
        [manual_job, workday_job, undecided], "10:00 am", 3, telegram=None
    )
    with db.connect() as conn:
        db.set_decision(conn, run_id, 1, "manual")   # Stripe, ranked first

    with db.connect() as conn:
        pending = {j.company for j, _ in db.handed_off_unreported(conn)}
    # The undecided job is not hers yet; the manual pick and Workday job are.
    assert pending == {"Stripe", "Nvidia"}

    t = FakeTelegram()
    schedule.record_outcome(t, "q", manual_job.dedupe_key, "applied")
    with db.connect() as conn:
        pending = {j.company for j, _ in db.handed_off_unreported(conn)}
    assert pending == {"Nvidia"}


class FakeTelegram:
    """Just enough of Telegram to catch what record_outcome says."""

    chat_id = "1"
    jobs_chat_id = "1"

    def __init__(self):
        self.sent: list[str] = []
        self.acks: list[str] = []

    def send(self, text, buttons=None, chat_id=None):
        self.sent.append(text)
        return True

    def answer_callback(self, query_id, text=""):
        self.acks.append(text)
        return True


def test_outcome_callback_roundtrip() -> None:
    (label, data) = propose.outcome_buttons("abc123")[0][1][0], \
        propose.outcome_buttons("abc123")[0][1][1]
    assert propose.parse_outcome_callback(data) == ("abc123", "dup")
    assert propose.parse_outcome_callback("o:k:teleported") is None
    assert propose.parse_outcome_callback("d:1:2:auto") is None


def test_applied_outcome_marks_applied_and_says_so(isolated_db) -> None:
    from job_agent import schedule

    db = isolated_db
    job = make_job(0.8, "Nvidia")
    with db.connect() as conn:
        db.upsert_job(conn, job)
    t = FakeTelegram()
    schedule.record_outcome(t, "q1", job.dedupe_key, "applied")
    with db.connect() as conn:
        assert db.job_by_key(conn, job.dedupe_key).status == JobStatus.APPLIED
    # Tracker is unconfigured in tests; the message must say so honestly
    # rather than claiming a row was written.
    assert any("marked applied" in m for m in t.sent)


def test_repost_outcome_never_touches_the_tracker(isolated_db) -> None:
    """"Already applied" records the status but explicitly NOT a tracker row."""
    from job_agent import schedule

    db = isolated_db
    job = make_job(0.8, "Nvidia")
    with db.connect() as conn:
        db.upsert_job(conn, job)
    t = FakeTelegram()
    schedule.record_outcome(t, "q1", job.dedupe_key, "dup")
    with db.connect() as conn:
        assert db.job_by_key(conn, job.dedupe_key).status == JobStatus.APPLIED
    assert any("NOT" in m and "Jobtracker" in m for m in t.sent)


def test_ignore_outcome_skips_the_job(isolated_db) -> None:
    from job_agent import schedule

    db = isolated_db
    job = make_job(0.8, "Nvidia")
    with db.connect() as conn:
        db.upsert_job(conn, job)
    t = FakeTelegram()
    schedule.record_outcome(t, "q1", job.dedupe_key, "ignore")
    with db.connect() as conn:
        assert db.job_by_key(conn, job.dedupe_key).status == JobStatus.SKIPPED


def test_second_tap_does_not_double_record(isolated_db) -> None:
    """Taps arrive twice (retries, impatience); applied must be idempotent."""
    from job_agent import schedule

    db = isolated_db
    job = make_job(0.8, "Nvidia")
    with db.connect() as conn:
        db.upsert_job(conn, job)
    t = FakeTelegram()
    schedule.record_outcome(t, "q1", job.dedupe_key, "applied")
    before = len(t.sent)
    schedule.record_outcome(t, "q2", job.dedupe_key, "applied")
    assert len(t.sent) == before  # acked, but nothing re-recorded
    assert "Already recorded" in t.acks[-1]


def test_manual_mode_still_overrides_everything(isolated_db) -> None:
    from job_agent import schedule
    from job_agent.propose import Mode

    db = isolated_db
    jobs = [make_job(0.79, "Stripe")]
    with db.connect() as conn:
        db.upsert_job(conn, jobs[0])
    run_id, _ = schedule.open_batch(jobs, "10:00 am", 1, telegram=None)
    with db.connect() as conn:
        db.set_decision(conn, run_id, 1, "auto")
    assert schedule.to_fill(run_id, Mode.MANUAL) == []


def test_rest_ignore_covers_the_whole_batch(isolated_db) -> None:
    """Isolating one job must not mean typing `ignore` five times."""
    from job_agent import schedule
    from job_agent.propose import Mode

    db = isolated_db
    jobs = [make_job(0.79 - i / 100, f"Co{i}") for i in range(6)]
    with db.connect() as conn:
        for job in jobs:
            db.upsert_job(conn, job)
    run_id, _ = schedule.open_batch(jobs, "10:00 am", 6, telegram=None)

    with db.connect() as conn:
        db.set_decision(conn, run_id, 4, "auto")
        # Every tier, not only <70% — all six here are >=70%.
        assert db.decide_remaining(conn, run_id, "ignore") == 5

    assert [k.split("-")[0] for _, k in schedule.to_fill(run_id, Mode.AUTO)] == ["Co3"]


# --------------------------------------------------------------------------
# buttons
# --------------------------------------------------------------------------


def test_callback_data_fits_telegrams_64_byte_cap() -> None:
    """Over 64 bytes and Telegram rejects the whole message."""
    rows = propose.job_buttons(9999, 999) + propose.control_buttons(9999)
    for row in rows:
        for _label, data in row:
            assert len(data.encode()) <= 64, data


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("d:4:2:auto", ("d", 4, 2, "auto")),
        ("d:4:2:ignore", ("d", 4, 2, "ignore")),
        ("m:4:manual", ("m", 4, None, "manual")),
        ("b:4:ignore", ("b", 4, None, "ignore")),
        ("nonsense", None),
        ("d:4:2", None),
        ("d:x:2:auto", None),
        ("", None),
    ],
)
def test_parse_callback(data: str, expected) -> None:
    assert propose.parse_callback(data) == expected


def test_a_tap_from_an_older_batch_is_rejected() -> None:
    """Yesterday's message keeps its buttons; they must not act on today."""
    kind, run_id, ordinal, value = propose.parse_callback("d:1:2:auto")
    assert run_id == 1  # collect() compares this against its own run_id
    assert (kind, ordinal, value) == ("d", 2, "auto")


def test_every_job_offers_all_three_choices() -> None:
    (row,) = propose.job_buttons(1, 1)
    assert [data.rsplit(":", 1)[-1] for _, data in row] == [
        "auto", "manual", "ignore"
    ]
