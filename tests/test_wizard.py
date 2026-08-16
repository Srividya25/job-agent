"""The wizard's pure parts: .env editing and resume registration."""

from __future__ import annotations

import pytest
import yaml

from job_agent import wizard
from job_agent.listen import parse_resume_caption
from job_agent.wizard import upsert_env


@pytest.fixture
def tmp_profile(tmp_path, monkeypatch):
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({
        "identity": {"first_name": "Jane", "last_name": "Doe",
                     "email": "j@e.co"},
        "resumes": [{"label": "general", "path": "profile/a.pdf",
                     "target_roles": []}],
    }))
    monkeypatch.setattr(wizard, "PROFILE_PATH", path)
    return path


def test_register_resume_appends(tmp_profile) -> None:
    assert wizard.register_resume(
        "profile/ml.pdf", "ml", ["ML Engineer"]
    ) == ""
    data = yaml.safe_load(tmp_profile.read_text())
    assert data["resumes"][-1] == {
        "label": "ml", "path": "profile/ml.pdf", "target_roles": ["ML Engineer"]
    }
    assert data["resumes"][0]["label"] == "general"  # untouched


def test_register_resume_refuses_duplicate_label(tmp_profile) -> None:
    problem = wizard.register_resume("profile/x.pdf", "general", [])
    assert "already exists" in problem
    assert len(yaml.safe_load(tmp_profile.read_text())["resumes"]) == 1


@pytest.mark.parametrize(
    ("caption", "expected"),
    [("ml: ML Engineer, Data Scientist", ("ml", ["ML Engineer", "Data Scientist"])),
     ("frontend: Frontend Engineer", ("frontend", ["Frontend Engineer"])),
     ("", ("", [])),
     ("just some words", ("", []))],
)
def test_resume_caption_parsing(caption, expected) -> None:
    assert parse_resume_caption(caption) == expected


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
