"""Tier 3: both providers, the fallback chain, and the honesty guardrails.

No network anywhere — `post` is injected. What matters here:
  - a null from the model stays unresolved (never invented into an answer)
  - a choice answer the field's own options reject is dropped
  - anthropic failing rolls over to qwen in auto mode, and a pinned
    provider never rolls anywhere
  - LLM answers carry Tier.LLM, so is_confident refuses them
"""

from __future__ import annotations

import json

import httpx
import pytest

from job_agent.config import Identity, Profile
from job_agent.forms.extract import FormField
from job_agent.resolve import llm
from job_agent.resolve.engine import Tier, resolve_fields


@pytest.fixture
def profile() -> Profile:
    return Profile(
        identity=Identity(
            first_name="Jane", last_name="Doe", email="jane.doe@example.com"
        )
    )


def fields() -> list[FormField]:
    return [
        FormField(ref="#auth", label="Are you authorized to work in the US?",
                  type="radio", options=["Yes", "No"]),
        FormField(ref="#color", label="Favorite color?", type="text"),
    ]


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body,
                          request=httpx.Request("POST", "http://test"))


def anthropic_ok(payload: dict):
    def post(url, **kwargs):
        assert "api.anthropic.com" in url
        # The request must ask for constrained JSON, not hope for it.
        assert kwargs["json"]["output_config"]["format"]["type"] == "json_schema"
        return _response(200, {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(payload)}],
        })
    return post


def ollama_ok(payload: dict):
    def post(url, **kwargs):
        assert "11434" in url or "localhost" in url
        assert kwargs["json"]["format"] == llm._SCHEMA
        return _response(200, {"message": {"content": json.dumps(payload)}})
    return post


# --------------------------------------------------------------------------
# parsing and guardrails
# --------------------------------------------------------------------------


def test_null_answers_are_dropped():
    parsed = llm._parse(json.dumps({"answers": [
        {"ref": "#a", "value": "Yes"},
        {"ref": "#b", "value": None},
        {"ref": "#c", "value": "  "},
    ]}))
    assert parsed == {"#a": "Yes"}


def test_garbage_json_parses_to_nothing():
    assert llm._parse("the model rambled instead") == {}
    assert llm._parse(None) == {}


def test_prompt_carries_options_verbatim(profile):
    prompt = llm.build_prompt(profile, fields())
    assert "Are you authorized to work in the US?" in prompt
    assert '["Yes", "No"]' in prompt
    assert "jane.doe@example.com" in prompt


def test_prompt_carries_a_bounded_jd_excerpt(profile):
    """Essay drafts must be grounded in the posting, not the model's
    imagination — so the JD travels with the prompt, but bounded."""
    prompt = llm.build_prompt(
        profile, fields(), company="Plaid", title="SWE",
        description="We build financial infrastructure. " * 200,
    )
    assert "JOB DESCRIPTION" in prompt
    assert "financial infrastructure" in prompt
    start = prompt.index("JOB DESCRIPTION")
    end = prompt.index("FORM FIELDS")
    assert (end - start) <= llm.MAX_JD_CHARS + 100


def test_no_description_no_jd_section(profile):
    assert "JOB DESCRIPTION" not in llm.build_prompt(profile, fields())


def test_system_prompt_asks_for_reviewed_drafts_not_silence(profile):
    """Rule 4 changed from 'essays: null' to 'short reviewed draft'. The
    guardrails stay: nothing invented, and the human reviews every draft."""
    assert "draft" in llm._SYSTEM.lower()
    assert "reviews" in llm._SYSTEM.lower()
    assert "invented" in llm._SYSTEM.lower()


def test_profile_facts_omit_blank_lines(profile):
    facts = llm.profile_facts(profile)
    assert "Jane Doe" in facts
    assert "Phone" not in facts          # unset fields never reach the model
    assert "Veteran" not in facts


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------


def test_anthropic_happy_path(profile, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(llm, "anthropic_ready", lambda: True)
    monkeypatch.setattr(llm, "load_secrets", lambda: type(
        "S", (), {"anthropic_api_key": "sk-ant-test"})())
    post = anthropic_ok({"answers": [{"ref": "#auth", "value": "Yes"},
                                     {"ref": "#color", "value": None}]})
    answers, provider = llm.answer_fields(profile, fields(), post=post)
    assert provider == "anthropic"
    assert answers == {"#auth": "Yes"}


def test_qwen_happy_path(profile, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    post = ollama_ok({"answers": [{"ref": "#auth", "value": "Yes"}]})
    answers, provider = llm.answer_fields(profile, fields(), post=post)
    assert provider == "qwen"
    assert answers == {"#auth": "Yes"}


def test_auto_falls_back_to_qwen_when_anthropic_fails(profile, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setattr(llm, "anthropic_ready", lambda: True)
    monkeypatch.setattr(llm, "load_secrets", lambda: type(
        "S", (), {"anthropic_api_key": "sk-ant-test"})())

    def post(url, **kwargs):
        if "api.anthropic.com" in url:
            return _response(429, {"error": {"message": "rate limited"}})
        return _response(200, {"message": {"content": json.dumps(
            {"answers": [{"ref": "#auth", "value": "Yes"}]})}})

    events = []
    answers, provider = llm.answer_fields(profile, fields(), post=post,
                                          on_event=events.append)
    assert provider == "qwen"
    assert answers == {"#auth": "Yes"}
    assert any("anthropic" in e for e in events)  # the failure was reported


def test_pinned_provider_never_falls_back(profile, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(llm, "anthropic_ready", lambda: True)
    monkeypatch.setattr(llm, "load_secrets", lambda: type(
        "S", (), {"anthropic_api_key": "sk-ant-test"})())

    calls = []
    def post(url, **kwargs):
        calls.append(url)
        return _response(500, {})

    answers, provider = llm.answer_fields(profile, fields(), post=post)
    assert answers == {} and provider == ""
    assert all("api.anthropic.com" in u for u in calls)  # never tried Ollama


def test_refusal_is_a_failure_not_an_answer(profile, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(llm, "load_secrets", lambda: type(
        "S", (), {"anthropic_api_key": "sk-ant-test"})())
    post = lambda url, **k: _response(200, {"stop_reason": "refusal", "content": []})
    answers, provider = llm.answer_fields(profile, fields(), post=post)
    assert answers == {} and provider == ""


def test_off_disables_tier3(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "off")
    assert llm.available() is False


# --------------------------------------------------------------------------
# engine wiring — fields the rules genuinely cannot answer
# --------------------------------------------------------------------------


def novel_fields() -> list[FormField]:
    return [
        FormField(ref="#team", label="Which team excites you most?",
                  type="radio", options=["Payments", "Infrastructure"]),
        FormField(ref="#hobby", label="Tell us something surprising about you",
                  type="textarea"),
    ]


def test_engine_marks_llm_answers_unconfident(profile, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(
        llm, "answer_fields",
        lambda prof, flds, **kw: ({"#team": "Payments"}, "qwen"),
    )
    resolution = resolve_fields(novel_fields(), profile, use_cache=False, use_llm=True)
    answer = next(a for a in resolution.answers if a.ref == "#team")
    assert answer.tier is Tier.LLM
    assert answer.is_confident is False        # ADR-004: always reviewed
    assert resolution.all_confident is False
    # The unanswered free-text field stays unresolved, to be asked.
    assert any(f.ref == "#hobby" for f in resolution.unresolved)


def test_engine_rejects_choice_answer_not_in_options(profile, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(
        llm, "answer_fields",
        lambda prof, flds, **kw: ({"#team": "Marketing"}, "qwen"),
    )
    resolution = resolve_fields(novel_fields(), profile, use_cache=False, use_llm=True)
    assert not any(a.ref == "#team" for a in resolution.answers)
    assert any(f.ref == "#team" for f in resolution.unresolved)


def test_engine_default_never_calls_llm(profile, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("resolve_fields called the LLM without opt-in")
    monkeypatch.setattr(llm, "available", boom)
    resolution = resolve_fields(novel_fields(), profile, use_cache=False)
    assert any(f.ref == "#hobby" for f in resolution.unresolved)


def test_learn_never_caches_llm_answers(profile, monkeypatch):
    from job_agent.resolve import cache, engine

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(
        llm, "answer_fields",
        lambda prof, flds, **kw: ({"#team": "Payments"}, "anthropic"),
    )
    remembered = []
    monkeypatch.setattr(cache, "remember", lambda *a, **k: remembered.append(a))
    resolution = resolve_fields(novel_fields(), profile, use_cache=False, use_llm=True)
    engine.learn(resolution)
    assert remembered == []                    # a guess never poisons the cache
