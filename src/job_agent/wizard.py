"""First-run wizard: from a fresh clone to a working setup, one question at
a time.

`job-agent init` copies the example profile and leaves the user alone with a
YAML file; this walks them through it instead — profile, resumes, Telegram
bot (including discovering the chat id for them), and the optional keys —
then verifies the result against their actual resume PDFs.

Every prompt defaults to whatever the profile already says, so re-running
the wizard edits rather than restarts.
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

import httpx
import typer
import yaml

from .config import EXAMPLE_PATH, PROFILE_PATH, ROOT
from .notify.telegram import API

ENV_PATH = ROOT / ".env"


# --------------------------------------------------------------------------
# .env editing (pure, tested)
# --------------------------------------------------------------------------


def upsert_env(text: str, key: str, value: str) -> str:
    """Set key=value in .env text.

    Replaces an existing assignment, or an existing commented-out placeholder
    ("# ANTHROPIC_API_KEY=sk-ant-..."), so the file's comments and grouping
    survive being edited by the wizard. Appends when the key is new.
    """
    pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    out, done = [], False
    for line in text.splitlines():
        if not done and pattern.match(line):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def _write_env(updates: dict[str, str]) -> None:
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    for key, value in updates.items():
        text = upsert_env(text, key, value)
    ENV_PATH.write_text(text)


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


# --------------------------------------------------------------------------
# profile sections
# --------------------------------------------------------------------------


def _identity(data: dict, console) -> None:
    console.print("\n[bold]— You —[/]")
    i = data.setdefault("identity", {})
    i["first_name"] = typer.prompt("First name", default=i.get("first_name", ""))
    i["last_name"] = typer.prompt("Last name", default=i.get("last_name", ""))
    i["email"] = typer.prompt("Email", default=i.get("email", ""))
    i["phone"] = str(typer.prompt("Phone (digits only)", default=i.get("phone", "")))
    i["linkedin"] = typer.prompt(
        "LinkedIn URL (blank to skip)", default=i.get("linkedin", "")
    )
    a = data.setdefault("address", {})
    a["city"] = typer.prompt("City", default=a.get("city", ""))
    a["state"] = typer.prompt("State", default=a.get("state", ""))
    a["country"] = typer.prompt("Country", default=a.get("country", "United States"))


_AUTH_CHOICES = [
    ("US citizen or green card", False),
    ("H-1B", True),
    ("F-1 OPT / STEM OPT", True),
    ("Other", True),
]


def _work_auth(data: dict, console) -> None:
    console.print("\n[bold]— Work authorization —[/]")
    console.print(
        "This drives the sponsorship filter: if you need sponsorship, "
        "postings that refuse it are dropped before you ever see them."
    )
    for n, (label, _) in enumerate(_AUTH_CHOICES, start=1):
        console.print(f"  {n}. {label}")
    w = data.setdefault("work_authorization", {})
    pick = int(typer.prompt("Which fits?", default="3"))
    label, needs = _AUTH_CHOICES[min(max(pick, 1), len(_AUTH_CHOICES)) - 1]
    w["status"] = (
        typer.prompt("Describe it", default=w.get("status", ""))
        if label == "Other" else label
    )
    w["needs_sponsorship"] = typer.confirm(
        "Will you need visa sponsorship (now or later)?", default=needs
    )
    w["authorized_now"] = typer.confirm(
        "Are you authorized to work right now?", default=True
    )


def _background(data: dict, console) -> None:
    console.print("\n[bold]— Background —[/]")
    edu = (data.get("education") or [{}])[0]
    edu["school"] = typer.prompt("School", default=edu.get("school", ""))
    edu["degree"] = typer.prompt("Degree", default=edu.get("degree", ""))
    edu["field"] = typer.prompt("Field of study", default=edu.get("field", ""))
    edu["graduation_year"] = int(typer.prompt(
        "Graduation year", default=str(edu.get("graduation_year", ""))
    ))
    data["education"] = [edu] + (data.get("education") or [{}])[1:]

    x = data.setdefault("experience", {})
    x["years"] = float(typer.prompt(
        "Years of experience", default=str(x.get("years", 0))
    ))
    x["current_title"] = typer.prompt(
        "Current title", default=x.get("current_title", "")
    )

    s = data.setdefault("skills", {})
    s["must_have"] = _csv(typer.prompt(
        "Skills you want to be hired for (comma-separated)",
        default=", ".join(s.get("must_have", [])),
    ))
    s["nice_to_have"] = _csv(typer.prompt(
        "Nice-to-have skills (comma-separated)",
        default=", ".join(s.get("nice_to_have", [])),
    ))

    p = data.setdefault("preferences", {})
    p["titles"] = _csv(typer.prompt(
        "Job titles to search for (comma-separated)",
        default=", ".join(p.get("titles", [])),
    ))
    p["locations"] = _csv(typer.prompt(
        "Locations (comma-separated; include Remote if wanted)",
        default=", ".join(p.get("locations", [])),
    ))


def _resumes(data: dict, console) -> None:
    console.print("\n[bold]— Resumes —[/]")
    console.print(
        "PDFs, ideally inside profile/ (that folder is gitignored). "
        "Several versions are fine — the agent picks the best per job."
    )
    resumes: list[dict] = []
    while True:
        raw = typer.prompt(
            "Path to a resume PDF" + (" (blank to finish)" if resumes else ""),
            default="",
        ).strip()
        if not raw:
            if resumes:
                break
            console.print("[yellow]At least one resume is needed.[/]")
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = ROOT / raw
        if not path.exists():
            console.print(f"[red]Not found:[/] {path}")
            continue
        label = typer.prompt("Label for it", default=path.stem.lower())
        try:
            rel = str(path.relative_to(ROOT))
        except ValueError:
            dest = ROOT / "profile" / path.name
            if typer.confirm(f"Copy it into profile/ as {dest.name}?", default=True):
                shutil.copy(path, dest)
                rel = f"profile/{dest.name}"
            else:
                rel = str(path)
        resumes.append({"label": label, "path": rel, "target_roles": []})
    data["resumes"] = resumes


# --------------------------------------------------------------------------
# telegram
# --------------------------------------------------------------------------


def _telegram(console) -> dict[str, str]:
    console.print("\n[bold]— Telegram (your approval channel) —[/]")
    console.print(
        "The ranked lists, form screenshots, and Submit buttons all arrive "
        "here.\n"
        "  1. In Telegram, message [bold]@BotFather[/] → /newbot → pick a name\n"
        "  2. BotFather replies with a token like 123456:ABC-…\n"
    )
    while True:
        token = typer.prompt("Bot token (blank to skip Telegram)", default="").strip()
        if not token:
            return {}
        try:
            r = httpx.get(f"{API}/bot{token}/getMe", timeout=10.0)
            if r.status_code == 200 and r.json().get("ok"):
                name = r.json()["result"].get("username", "your bot")
                console.print(f"  [green]Token works — that's @{name}.[/]")
                break
        except httpx.HTTPError:
            pass
        console.print("[red]Telegram rejected that token — check for typos.[/]")

    console.print(
        "\nNow open Telegram and send your bot any message (e.g. \"hi\") "
        "so I can learn your chat id. Waiting up to 2 minutes…"
    )
    chat_id = ""
    deadline = time.time() + 120
    while time.time() < deadline and not chat_id:
        try:
            r = httpx.get(f"{API}/bot{token}/getUpdates", timeout=10.0)
            for update in reversed(r.json().get("result", [])):
                chat = (update.get("message") or {}).get("chat") or {}
                if chat.get("id"):
                    chat_id = str(chat["id"])
                    break
        except (httpx.HTTPError, ValueError):
            pass
        if not chat_id:
            time.sleep(3)
    if not chat_id:
        console.print(
            "[yellow]No message arrived. Add TELEGRAM_CHAT_ID to .env "
            "yourself later — see .env.example.[/]"
        )
        return {"TELEGRAM_BOT_TOKEN": token}

    httpx.post(
        f"{API}/bot{token}/sendMessage",
        json={"chat_id": chat_id,
              "text": "👋 job-agent connected. This is where your job lists "
                      "and reviews will arrive."},
        timeout=10.0,
    )
    console.print(f"  [green]Connected — test message sent (chat {chat_id}).[/]")
    return {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id}


def _optional_keys(console) -> dict[str, str]:
    updates: dict[str, str] = {}
    console.print("\n[bold]— Optional —[/]")
    key = typer.prompt(
        "Anthropic API key, for Claude on novel form questions "
        "(blank = use a local Ollama model or skip)", default="",
    ).strip()
    if key:
        updates["ANTHROPIC_API_KEY"] = key
        updates["LLM_PROVIDER"] = "auto"
    if typer.confirm(
        "Set up the confirmation-email watcher (Gmail app password)?",
        default=False,
    ):
        updates["GMAIL_ADDRESS"] = typer.prompt("Gmail address")
        console.print(
            "  Create an app password at myaccount.google.com/apppasswords"
        )
        updates["GMAIL_APP_PASSWORD"] = typer.prompt("App password")
    return updates


# --------------------------------------------------------------------------
# the wizard
# --------------------------------------------------------------------------


def run(console) -> None:
    console.print("[bold]job-agent setup[/]\n")
    console.print(
        "A few questions, then a working agent. Answers land in "
        "profile/profile.yaml and .env — both gitignored, both yours to "
        "edit by hand later. Enter keeps the shown default."
    )

    if not PROFILE_PATH.exists():
        shutil.copy(EXAMPLE_PATH, PROFILE_PATH)
    data = yaml.safe_load(PROFILE_PATH.read_text()) or {}

    _identity(data, console)
    _work_auth(data, console)
    _background(data, console)
    _resumes(data, console)

    PROFILE_PATH.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    console.print(f"\n[green]Wrote[/] {PROFILE_PATH}")

    env_updates = _telegram(console) | _optional_keys(console)
    if env_updates:
        _write_env(env_updates)
        console.print(f"[green]Wrote[/] {ENV_PATH}")

    # The check that exists because a profile once contradicted the resume.
    console.print("\n[bold]— Checking profile against your resumes —[/]")
    try:
        from .config import load_profile
        from .match.resume import load_resumes
        from .profile_check import Level, check_all

        load_profile.cache_clear()
        profile = load_profile()
        problems = 0
        for label, findings in check_all(
            profile, load_resumes(profile.resumes)
        ).items():
            for f in findings:
                colour = "red" if f.level is Level.ERROR else "yellow"
                console.print(f"  [{colour}]{label}: {f.field} — {f.detail}[/]")
                problems += 1
        if not problems:
            console.print("  [green]Profile and resumes agree.[/]")
        else:
            console.print(
                "  Fix these in profile/profile.yaml — the agent refuses to "
                "apply while the profile contradicts the resume."
            )
    except Exception as exc:  # noqa: BLE001 - the wizard should finish anyway
        console.print(f"  [yellow]Could not run the check: {exc}[/]")

    console.print(
        "\n[bold]Next steps[/]\n"
        "  job-agent data      # download reference datasets\n"
        "  job-agent chrome    # open the dedicated browser profile\n"
        "  job-agent batch     # discover, propose, and fill your picks\n\n"
        "The agent never presses Submit — every application waits for you."
    )
