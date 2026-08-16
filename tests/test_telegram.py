"""Reply parsing — the part that must not misroute an answer.

No network: these are all pure functions.
"""

from __future__ import annotations

import pytest

from job_agent.notify.telegram import format_questions, parse_reply


@pytest.mark.parametrize(
    ("text", "ordinal", "answer"),
    [
        ("2 Yes", 2, "Yes"),
        ("2. Yes", 2, "Yes"),
        ("2) yes please", 2, "yes please"),
        ("#2 - Yes", 2, "Yes"),
        ("  3   No  ", 3, "No"),
        ("1 I have not worked there before", 1, "I have not worked there before"),
        # Multi-line answers survive (cover-letter style text).
        ("4 line one\nline two", 4, "line one\nline two"),
    ],
)
def test_parse_reply(text: str, ordinal: int, answer: str) -> None:
    reply = parse_reply(text)
    assert reply is not None
    assert reply.ordinal == ordinal
    assert reply.answer == answer


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "yes",            # no ordinal — cannot tell which question
        "2",              # ordinal but no answer
        "2   ",
        "hello there",
        "what's the status?",
    ],
)
def test_parse_reply_rejects_ambiguous(text: str) -> None:
    """Better to ignore a message than to attach it to the wrong question."""
    assert parse_reply(text) is None


def test_ordinal_is_not_swallowed_by_answer() -> None:
    """A leading number must be the ordinal, not part of the answer."""
    reply = parse_reply("1 2 years")
    assert reply is not None
    assert reply.ordinal == 1
    assert reply.answer == "2 years"


def test_format_questions_is_numbered_and_instructive() -> None:
    text = format_questions(
        "Twilio",
        "Software Engineer (L2)",
        [(1, "Have you worked here before?"), (2, "Acknowledge privacy policy")],
        url="https://example.com/job",
    )
    assert "1. Have you worked here before?" in text
    assert "2. Acknowledge privacy policy" in text
    assert "Reply like:" in text
    # Round-trips: what we tell the user to send must actually parse.
    reply = parse_reply("2 Yes")
    assert reply is not None and reply.ordinal == 2


# --------------------------------------------------------------------------
# time windows
# --------------------------------------------------------------------------

from job_agent.models import parse_since  # noqa: E402


@pytest.mark.parametrize(
    ("value", "hours"),
    [
        ("24h", 24), ("today", 24), ("day", 24),
        ("3d", 72), ("3days", 72), ("2 days", 48),
        ("1w", 168), ("week", 168), ("7d", 168),
        ("30d", 720), ("month", 720),
        ("all", 0), ("", 0), (None, 0),
        ("  3D  ", 72),
    ],
)
def test_parse_since(value, hours) -> None:
    assert parse_since(value) == hours


@pytest.mark.parametrize("value", ["yesterday", "soon", "3x", "-2d", "abc"])
def test_parse_since_rejects_garbage(value: str) -> None:
    """A typo must not silently widen the search to everything."""
    with pytest.raises(ValueError):
        parse_since(value)


# --------------------------------------------------------------------------
# multi-answer messages
# --------------------------------------------------------------------------

from job_agent.notify.telegram import parse_replies  # noqa: E402


def test_several_answers_in_one_message() -> None:
    """Answering two questions at once must not merge them.

    Real reply seen in use: "1. no\\n2. yes". Parsed as one answer it put
    "no\\n2. yes" against question 1 and lost question 2 entirely.
    """
    out = parse_replies("1. no\n2. yes")
    assert [(r.ordinal, r.answer) for r in out] == [(1, "no"), (2, "yes")]


def test_three_answers() -> None:
    out = parse_replies("1 No\n2 Yes\n5 Bachelor's")
    assert [(r.ordinal, r.answer) for r in out] == [
        (1, "No"), (2, "Yes"), (5, "Bachelor's")
    ]


def test_single_multiline_answer_stays_whole() -> None:
    """One ordinal means the rest is one answer, not several."""
    out = parse_replies("3 I heard about this role\nfrom a friend at the company")
    assert len(out) == 1
    assert out[0].ordinal == 3
    assert "friend at the company" in out[0].answer


def test_multi_answer_ignores_unnumbered_noise() -> None:
    out = parse_replies("here you go\n1 No\n2 Yes")
    assert [(r.ordinal, r.answer) for r in out] == [(1, "No"), (2, "Yes")]


@pytest.mark.parametrize(
    "text", ["hey what's up", "hi", "ok", "thanks", "done", "/start", "status"]
)
def test_chatter_is_not_treated_as_an_answer(text: str) -> None:
    """With one question open a bare message is attached to it, so anything
    conversational must be filtered — answering a real application with
    "hey" is far worse than ignoring a message."""
    assert parse_replies(text) == []


def test_a_question_is_not_an_answer() -> None:
    assert parse_replies("what is the status?") == []


def test_bare_answer_is_kept_with_ordinal_zero() -> None:
    """Sent without a number; the caller attaches it when only one is open."""
    out = parse_replies("Heterosexual")
    assert len(out) == 1
    assert out[0].ordinal == 0
    assert out[0].answer == "Heterosexual"


# --------------------------------------------------------------------------
# reply consumption
# --------------------------------------------------------------------------

from job_agent import run as _run  # noqa: E402
from job_agent.models import JobStatus  # noqa: E402
from job_agent.notify.telegram import Reply  # noqa: E402
from job_agent.store import db as _db  # noqa: E402


class _FakeTelegram:
    def __init__(self, replies):
        self._replies = replies
        self.sent = []

    def poll(self, offset):
        return self._replies, offset + len(self._replies) + 1

    def send(self, text):
        self.sent.append(text)
        return True


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """Point every store at a temp dir.

    Without this the suite reads and writes the real data/ — a live pending
    question with the same ordinal was matched instead of the seeded one, so
    the test failed for reasons that had nothing to do with the code.
    """
    import job_agent.config as cfg

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    yield tmp_path


def test_replies_close_questions_and_requeue(isolated_data) -> None:
    key = "t_match"
    with _db.connect() as conn:
        _db.clear_pending(conn, key)
        _db.add_pending(conn, key, 1, "Worked here before?", "#a", "select", "x.com")
        _db.add_pending(conn, key, 2, "Acknowledge policy", "#b", "checkbox", "x.com")

    _run._save_offset(0)
    tg = _FakeTelegram([Reply(1, "No", 1, "1 No"), Reply(2, "Yes", 2, "2 Yes")])
    assert _run.consume_replies(tg) == 2

    with _db.connect() as conn:
        remaining = [r for r in _db.open_questions(conn) if r["dedupe_key"] == key]
        assert remaining == []
        _db.clear_pending(conn, key)


def test_unmatched_reply_is_reported_not_silently_dropped(isolated_data) -> None:
    """Advancing the offset makes Telegram discard the update permanently.

    An answer that matched nothing is therefore unrecoverable, so it must be
    surfaced. This happened for real: replies to a message that was never
    persisted as questions vanished with no trace.
    """
    _run._save_offset(0)
    tg = _FakeTelegram([Reply(97, "No", 1, "97 No")])
    assert _run.consume_replies(tg) == 0
    assert tg.sent, "unmatched answer was dropped without telling anyone"
    assert "97" in tg.sent[0]


# --------------------------------------------------------------------------
# asking a question a person can answer
# --------------------------------------------------------------------------


def test_a_choice_question_lists_its_choices() -> None:
    """Without the options she has to guess the exact wording the form takes."""
    from job_agent.notify.telegram import format_question

    body = format_question(
        1, "Preferred Work Location", "checkbox",
        ["San Francisco HQ", "New York City Office", "Seattle Office"],
        company="Plaid", title="SWE",
    )
    assert "Preferred Work Location" in body
    assert "San Francisco HQ" in body and "Seattle Office" in body
    assert "Plaid" in body


def test_a_free_text_question_says_what_kind_of_answer() -> None:
    from job_agent.notify.telegram import format_question

    assert "a date" in format_question(2, "End Date", "date")
    assert "a link" in format_question(3, "Other URL", "url")


def test_question_buttons_stay_under_the_callback_cap() -> None:
    """Option text is far too long for callback_data; only the index travels."""
    from job_agent.notify.telegram import question_buttons

    long_options = [
        "I identify as one or more of the classifications of a protected veteran",
        "I am not a protected veteran",
    ]
    for row in question_buttons(999999, long_options):
        for _label, data in row:
            assert len(data.encode()) <= 64


def test_question_callback_roundtrip() -> None:
    from job_agent.notify.telegram import parse_question_callback, question_buttons

    (row,) = question_buttons(17, ["Yes", "No"])
    assert [parse_question_callback(d) for _, d in row] == [(17, 0), (17, 1)]
    assert parse_question_callback("nonsense") is None


def test_buttons_are_capped_so_a_long_list_stays_usable() -> None:
    from job_agent.notify.telegram import question_buttons

    rows = question_buttons(1, [f"Option {i}" for i in range(40)])
    assert sum(len(r) for r in rows) <= 8


def test_a_location_question_still_offers_taps() -> None:
    """Typeahead pickers expose no options, so a location question arrived
    with nothing to tap. These are the candidates a person would pick."""
    from job_agent.config import load_profile
    from job_agent.forms.extract import FormField
    from job_agent.models import ATS, Job, JobStatus
    from job_agent.run import suggestions

    profile = load_profile()
    job = Job(dedupe_key="k", company="Plaid", title="SWE",
              url="https://x", location="San Francisco HQ", ats=ATS.ASHBY,
              status=JobStatus.NEW)

    options = suggestions(
        FormField(ref="#c", label="Location (City)", type="text"), profile, job
    )
    assert "San Jose" in options
    assert "Remote" in options
    assert "San Francisco HQ" in options


def test_a_yes_no_question_offers_yes_and_no() -> None:
    from job_agent.config import load_profile
    from job_agent.forms.extract import FormField
    from job_agent.models import ATS, Job, JobStatus
    from job_agent.run import suggestions

    profile = load_profile()
    job = Job(dedupe_key="k", company="P", title="SWE", url="https://x",
              ats=ATS.ASHBY, status=JobStatus.NEW)
    options = suggestions(
        FormField(ref="#q", label="Have you ever worked here before?", type="text"),
        profile, job,
    )
    assert options == ["Yes", "No"]


def test_the_forms_own_options_always_win() -> None:
    """Never override real choices with guesses."""
    from job_agent.config import load_profile
    from job_agent.forms.extract import FormField
    from job_agent.models import ATS, Job, JobStatus
    from job_agent.run import suggestions

    profile = load_profile()
    job = Job(dedupe_key="k", company="P", title="SWE", url="https://x",
              ats=ATS.ASHBY, status=JobStatus.NEW)
    real = ["San Francisco HQ", "New York City Office"]
    assert suggestions(
        FormField(ref="#o", label="Preferred Work Location", type="checkbox",
                  options=real), profile, job,
    ) == real


def test_a_lone_checkbox_offers_yes_and_no_not_its_own_label() -> None:
    """"Still Student?" was offered as the only choice, so tapping it recorded
    "Still Student?" as the answer — ticking a claim that is not true."""
    from job_agent.config import load_profile
    from job_agent.forms.extract import FormField
    from job_agent.models import ATS, Job, JobStatus
    from job_agent.run import suggestions

    job = Job(dedupe_key="k", company="Plaid", title="SWE", url="https://x",
              ats=ATS.ASHBY, status=JobStatus.NEW)
    field = FormField(ref="#ss", label="Still Student?", type="checkbox",
                      options=["Still Student?"])
    assert suggestions(field, load_profile(), job) == ["Yes", "No"]


def test_a_real_checkbox_group_keeps_its_options() -> None:
    from job_agent.config import load_profile
    from job_agent.forms.extract import FormField
    from job_agent.models import ATS, Job, JobStatus
    from job_agent.run import suggestions

    job = Job(dedupe_key="k", company="Plaid", title="SWE", url="https://x",
              ats=ATS.ASHBY, status=JobStatus.NEW)
    real = ["San Francisco HQ", "New York City Office"]
    field = FormField(ref="#o", label="Preferred Work Location", type="checkbox",
                      options=real)
    assert suggestions(field, load_profile(), job) == real


def test_cache_refuses_an_answer_that_is_just_the_question() -> None:
    from job_agent.resolve import cache

    cache.remember("Still Student?", "Still Student?", "test-scope", "checkbox")
    assert cache.lookup("Still Student?", "test-scope") is None
