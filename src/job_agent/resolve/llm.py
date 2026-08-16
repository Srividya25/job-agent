"""Tier 3 — a language model answers what the cache and rules could not.

Two providers, same contract:

    anthropic  Claude via the Messages API   needs ANTHROPIC_API_KEY, costs money
    qwen       a local model via Ollama      free, private, already installed

The provider is chosen by LLM_PROVIDER in .env (`job-agent llm use …` writes
it). `auto` tries Claude first and falls back to Ollama when Claude is out of
credit, rate-limited, or unreachable — so a spent token budget never stops the
agent, it just changes who answers.

Safety posture, regardless of provider:
  - Answers only from the profile facts handed to it. A field the facts cannot
    answer comes back null and stays unresolved — the model is told inventing
    is worse than blank (the Anna University lesson, in prompt form).
  - Every answer is Tier.LLM, which is_confident refuses, so nothing an LLM
    wrote can be submitted without her review (ADR-004).
  - engine.learn() only caches Tier.RULES answers, so an LLM guess can never
    poison the answers cache.

REST directly via httpx, same style as telegram.py — no SDK for one endpoint.
"""

from __future__ import annotations

import json
import os

import httpx

from ..config import Profile, load_secrets

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# Overridable via ANTHROPIC_MODEL — e.g. claude-haiku-4-5 at ~1/5 the price.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen3.6:latest"

# Local models are slow: qwen3.6 on a laptop takes ~2 minutes for one
# batched call even with thinking disabled. 90s timed out on every real
# call, which silently reduced Tier 3 to "unavailable" while the status
# table said available.
TIMEOUT = 240.0
MAX_FIELDS = 25  # one screen of questions; anything bigger is a broken extract

PROVIDERS = ("auto", "anthropic", "qwen", "off")

# The model must return exactly this shape; both providers can constrain to a
# JSON schema, which is what makes the reply parseable instead of hopeful.
_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "value": {"type": ["string", "null"]},
                },
                "required": ["ref", "value"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["answers"],
    "additionalProperties": False,
}

_SYSTEM = """\
You fill job application forms on behalf of one person, from a fact sheet.

Rules, in order of importance:
1. Answer ONLY from the facts given. If the facts do not contain the answer,
   return null for that field. A wrong answer on a job application is far
   worse than a blank one. Never guess, never invent, never embellish.
2. For a field that lists Options, the value must be copied EXACTLY from that
   list — same wording, same capitalization. If several options apply and the
   field allows it, join them with " | ". If no option fits the facts, null.
3. Yes/No questions about the person (work authorization, sponsorship,
   relocation, prior employment at the company) must come from the facts;
   if the facts are silent, null.
4. Free-text "why do you want to work here / describe your interest" style
   questions: write a SHORT first-person draft (2-4 sentences), grounded
   ONLY in the facts and the job description. Plain and specific; no
   flattery, no invented experiences, projects, or numbers. The person
   reviews and rewrites every draft before anything is submitted. If the
   facts offer nothing relevant to the question, null.
5. Return every field's ref, each exactly once."""

# How much of the job description travels with the prompt. Enough for a
# grounded "why here" draft; not the whole posting, which on Workday-scale
# JDs would dwarf the questions themselves.
MAX_JD_CHARS = 1500


class LLMError(RuntimeError):
    """The provider could not answer — carries which one and why."""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"{provider}: {reason}")


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def provider() -> str:
    """anthropic | qwen | auto — from .env, defaulting to auto."""
    value = (os.getenv("LLM_PROVIDER") or "auto").strip().lower()
    return value if value in PROVIDERS else "auto"


def anthropic_ready() -> bool:
    return bool(load_secrets().anthropic_api_key)


def ollama_ready() -> bool:
    """Whether Ollama is up. Cheap GET; never raises."""
    url = os.getenv("OLLAMA_URL", OLLAMA_URL).rstrip("/")
    try:
        r = httpx.get(f"{url}/api/tags", timeout=3.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def available() -> bool:
    """Whether Tier 3 can run at all, under the configured provider."""
    p = provider()
    if p == "off":
        return False
    if p == "anthropic":
        return anthropic_ready()
    if p == "qwen":
        return ollama_ready()
    return anthropic_ready() or ollama_ready()


def status() -> dict[str, object]:
    """What `job-agent llm` prints: who would answer, and why."""
    return {
        "provider": provider(),
        "anthropic_key": anthropic_ready(),
        "anthropic_model": os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL),
        "ollama_up": ollama_ready(),
        "ollama_model": os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
    }


# --------------------------------------------------------------------------
# prompt assembly
# --------------------------------------------------------------------------


def profile_facts(profile: Profile) -> str:
    """The fact sheet the model answers from. Only what the profile states.

    Deliberately not the resume text: the profile is the verified source
    (checked against the resumes by `job-agent check`), and a smaller sheet
    leaves less room for the model to free-associate.
    """
    i, a, w, x = profile.identity, profile.address, profile.work_authorization, profile.experience
    lines = [
        f"Name: {i.full_name}",
        f"Email: {i.email}",
        f"Phone: {i.phone}" if i.phone else "",
        f"LinkedIn: {i.linkedin}" if i.linkedin else "",
        f"GitHub: {i.github}" if i.github else "",
        f"Location: {', '.join(p for p in [a.city, a.state, a.country] if p)}",
        f"Work authorization: {w.status}" if w.status else "",
        f"Needs visa sponsorship: {'Yes' if w.needs_sponsorship else 'No'}",
        f"Authorized to work now: {'Yes' if w.authorized_now else 'No'}",
        f"Years of experience: {x.years:g}" if x.years else "",
        f"Current title: {x.current_title}" if x.current_title else "",
    ]
    for e in profile.education:
        parts = [e.school, e.degree, e.field]
        span = ""
        if e.start_month or e.start_year:
            span = f" ({e.start_month} {e.start_year or ''} – {e.end_month} {e.graduation_year or ''})".replace("  ", " ")
        lines.append("Education: " + ", ".join(p for p in parts if p) + span)
    if profile.skills.all:
        lines.append("Skills: " + ", ".join(profile.skills.all))
    eeo = profile.eeo
    if eeo.gender:
        lines.append(f"Gender: {eeo.gender}")
    if eeo.race:
        lines.append(f"Race/ethnicity: {eeo.race}")
    if eeo.veteran_status:
        lines.append(f"Veteran status: {eeo.veteran_status}")
    if eeo.disability_status:
        lines.append(f"Disability status: {eeo.disability_status}")
    return "\n".join(line for line in lines if line)


def build_prompt(
    profile: Profile, fields: list, company: str = "", title: str = "",
    description: str = "",
) -> str:
    lines = ["FACTS ABOUT THE APPLICANT:", profile_facts(profile), ""]
    if company or title:
        lines.append(f"THE JOB: {title} at {company}".strip())
        lines.append("")
    if description:
        lines += ["JOB DESCRIPTION (excerpt):", description[:MAX_JD_CHARS], ""]
    lines.append("FORM FIELDS TO ANSWER:")
    for field in fields[:MAX_FIELDS]:
        entry = f"- ref: {field.ref}\n  question: {field.label}\n  type: {field.type}"
        options = getattr(field, "options", None) or []
        if options:
            entry += "\n  options: " + json.dumps(options, ensure_ascii=False)
        lines.append(entry)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# providers — each takes the same (system, prompt) and returns {ref: value}
# --------------------------------------------------------------------------


def _parse(raw: str) -> dict[str, str]:
    """{"answers":[{"ref","value"},...]} -> {ref: value}, nulls dropped."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    out: dict[str, str] = {}
    for item in (data or {}).get("answers", []):
        if not isinstance(item, dict):
            continue
        ref, value = item.get("ref"), item.get("value")
        if ref and isinstance(value, str) and value.strip():
            out[str(ref)] = value.strip()
    return out


def ask_anthropic(prompt: str, post=httpx.post) -> dict[str, str]:
    key = load_secrets().anthropic_api_key
    if not key:
        raise LLMError("anthropic", "no ANTHROPIC_API_KEY in .env")
    model = os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
    try:
        r = post(
            ANTHROPIC_API,
            headers={
                "x-api-key": key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 2000,
                "system": _SYSTEM,
                "messages": [{"role": "user", "content": prompt}],
                "output_config": {"format": {"type": "json_schema", "schema": _SCHEMA}},
            },
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise LLMError("anthropic", f"network: {exc}") from exc

    if r.status_code in (401, 403):
        raise LLMError("anthropic", "key rejected — check ANTHROPIC_API_KEY")
    if r.status_code == 429:
        raise LLMError("anthropic", "rate limited")
    if r.status_code != 200:
        # 400 with a billing message is how "out of credit" usually surfaces.
        raise LLMError("anthropic", f"HTTP {r.status_code}: {r.text[:200]}")

    body = r.json()
    if body.get("stop_reason") == "refusal":
        raise LLMError("anthropic", "model refused the request")
    text = next(
        (b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"),
        "",
    )
    return _parse(text)


def ask_qwen(prompt: str, post=httpx.post) -> dict[str, str]:
    url = os.getenv("OLLAMA_URL", OLLAMA_URL).rstrip("/")
    model = os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    try:
        r = post(
            f"{url}/api/chat",
            json={
                "model": model,
                "stream": False,
                # Ollama accepts a JSON schema in `format` and constrains
                # decoding to it — same guarantee as Anthropic's json_schema.
                "format": _SCHEMA,
                # Thinking models burn the whole time budget reasoning
                # before emitting a token of JSON; these answers are lookups
                # from a fact sheet, not puzzles.
                "think": False,
                "options": {"temperature": 0},
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise LLMError("qwen", f"Ollama unreachable: {exc}") from exc
    if r.status_code != 200:
        raise LLMError("qwen", f"HTTP {r.status_code}: {r.text[:200]}")
    return _parse((r.json().get("message") or {}).get("content", ""))


# --------------------------------------------------------------------------
# the entry point engine.py calls
# --------------------------------------------------------------------------


def answer_fields(
    profile: Profile,
    fields: list,
    company: str = "",
    title: str = "",
    description: str = "",
    post=httpx.post,
    on_event=None,
) -> tuple[dict[str, str], str]:
    """Batched: one call answers every unresolved field it can.

    Returns ({ref: value}, provider_used). Empty dict when nothing could
    answer — the caller leaves those fields unresolved, exactly as before
    Tier 3 existed. Never raises: a dead provider degrades to "ask her",
    it does not crash a run.
    """
    if not fields:
        return {}, ""
    prompt = build_prompt(profile, fields, company, title, description)

    p = provider()
    order = {"anthropic": ["anthropic"], "qwen": ["qwen"]}.get(p, ["anthropic", "qwen"])

    for name in order:
        try:
            if name == "anthropic":
                if not anthropic_ready():
                    raise LLMError("anthropic", "no key")
                answers = ask_anthropic(prompt, post=post)
            else:
                answers = ask_qwen(prompt, post=post)
            if on_event:
                on_event(f"tier 3 ({name}): answered {len(answers)} of {len(fields)}")
            return answers, name
        except LLMError as exc:
            if on_event:
                on_event(f"tier 3: {exc}")
            continue
    return {}, ""
