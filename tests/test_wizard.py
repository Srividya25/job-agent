"""The wizard's pure parts: .env editing must never lose existing content."""

from __future__ import annotations

from job_agent.wizard import upsert_env


def test_appends_a_new_key() -> None:
    out = upsert_env("TELEGRAM_BOT_TOKEN=abc\n", "TELEGRAM_CHAT_ID", "42")
    assert "TELEGRAM_BOT_TOKEN=abc" in out
    assert out.rstrip().endswith("TELEGRAM_CHAT_ID=42")


def test_replaces_an_existing_assignment_in_place() -> None:
    out = upsert_env("A=1\nLLM_PROVIDER=off\nB=2\n", "LLM_PROVIDER", "auto")
    assert out.splitlines() == ["A=1", "LLM_PROVIDER=auto", "B=2"]


def test_uncomments_a_placeholder_line() -> None:
    """.env.example ships commented placeholders; the wizard fills them."""
    text = "# Tier 3:\n# ANTHROPIC_API_KEY=sk-ant-...\nB=2\n"
    out = upsert_env(text, "ANTHROPIC_API_KEY", "sk-ant-real")
    assert "ANTHROPIC_API_KEY=sk-ant-real" in out
    assert "sk-ant-..." not in out
    assert "# Tier 3:" in out  # comments survive


def test_only_the_first_matching_line_is_replaced_once() -> None:
    text = "K=old\n# K=older\n"
    out = upsert_env(text, "K", "new")
    assert out.splitlines() == ["K=new", "# K=older"]


def test_empty_file_gets_just_the_assignment() -> None:
    assert upsert_env("", "K", "v") == "K=v\n"


def test_key_names_do_not_match_their_own_prefix() -> None:
    """Setting GMAIL_ADDRESS must not clobber GMAIL_ADDRESS_BACKUP."""
    text = "GMAIL_ADDRESS_BACKUP=x@y.z\n"
    out = upsert_env(text, "GMAIL_ADDRESS", "a@b.c")
    assert "GMAIL_ADDRESS_BACKUP=x@y.z" in out
    assert "GMAIL_ADDRESS=a@b.c" in out
