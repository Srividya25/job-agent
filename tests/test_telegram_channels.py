"""Two-channel routing: job lists in one chat, everything else in the other.

No network — only the routing decisions.
"""

from __future__ import annotations

from job_agent.notify.telegram import Telegram


def test_one_chat_setup_is_unchanged() -> None:
    """Without TELEGRAM_JOBS_CHAT_ID everything routes exactly as before."""
    t = Telegram("tok", "111")
    assert t.jobs_chat_id == "111"
    assert t.allowed_chats == {"111"}


def test_jobs_channel_splits_when_configured() -> None:
    t = Telegram("tok", "111", jobs_chat_id="222")
    assert t.chat_id == "111"       # questions, reviews, submissions
    assert t.jobs_chat_id == "222"  # ranked job lists
    # Replies and taps from BOTH chats must be listened to, or a decision
    # tapped under a job posting would be silently dropped.
    assert t.allowed_chats == {"111", "222"}
