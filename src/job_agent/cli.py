"""job-agent command line."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import (
    EXAMPLE_PATH,
    PROFILE_PATH,
    ROOT,
    ProfileNotFound,
    data_dir,
    load_profile,
)
from .discover.base import make_client
from .discover.companies import SEED_COMPANIES, discover_many
from .filters import sponsorship
from .match.resume import load_resumes
from .models import ATS, JobStatus, Verdict, parse_since
from .pipeline import dedupe, fetch_all, persist, process
from .store import db

app = typer.Typer(
    add_completion=False,
    help="Finds jobs that fit you, filters the noise, and applies.",
)
companies_app = typer.Typer(help="Manage the target company list.")
data_app = typer.Typer(help="Download reference datasets.")
app.add_typer(companies_app, name="companies")
app.add_typer(data_app, name="data")

console = Console()
BOARDS_FILE = "boards.json"


def _load_boards() -> dict[str, tuple[ATS, str]]:
    path = data_dir() / BOARDS_FILE
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {name: (ATS(v["ats"]), v["slug"]) for name, v in raw.items()}


def _save_boards(boards: dict[str, tuple[ATS, str]]) -> None:
    path = data_dir() / BOARDS_FILE
    path.write_text(
        json.dumps(
            {n: {"ats": a.value, "slug": s} for n, (a, s) in sorted(boards.items())},
            indent=2,
        )
    )


def _profile_or_exit():
    try:
        return load_profile()
    except ProfileNotFound as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


# --------------------------------------------------------------------------


@app.command()
def init(
    wizard: bool = typer.Option(
        False, "--wizard", help="Guided setup: profile, resumes, Telegram, keys."
    ),
) -> None:
    """Create profile/profile.yaml from the example.

    With --wizard, walk through the whole first-run setup instead: profile
    questions, resume paths, the Telegram bot (chat id discovered for you),
    optional API keys — then verify the profile against your resumes.
    """
    if wizard:
        from . import wizard as wiz

        wiz.run(console)
        raise typer.Exit()
    if PROFILE_PATH.exists():
        console.print(f"[yellow]{PROFILE_PATH} already exists — leaving it alone.[/]")
        raise typer.Exit()
    shutil.copy(EXAMPLE_PATH, PROFILE_PATH)
    console.print(f"[green]Created[/] {PROFILE_PATH}")
    console.print(
        "Fill it in, then run: [bold]job-agent companies discover[/bold]\n"
        "(or run [bold]job-agent init --wizard[/bold] for a guided setup)"
    )


@app.command("confirm")
def confirm_cmd(
    days: int = typer.Option(14, help="How many days of inbox to scan."),
    quiet: bool = typer.Option(
        False, "--quiet", help="No Telegram message, terminal output only."
    ),
) -> None:
    """Check the inbox for employer emails about your applications.

    Confirmation emails ("thanks for applying") are recorded on the job —
    the strongest evidence a submission actually landed. Rejections and
    updates are reported but nothing is changed. Read-only on the mailbox.
    """
    from .notify.mail import check
    from .notify.telegram import Telegram

    telegram = None if quiet else Telegram.from_env()
    try:
        findings = check(
            days=days, telegram=telegram,
            on_event=lambda m: console.print(f"  [dim]{m}[/]"),
        )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    if not findings:
        with db.connect() as conn:
            waiting = len(db.applied_unconfirmed(conn))
        console.print(
            f"No news. {waiting} applied job(s) still without a "
            "confirmation email." if waiting else
            "No news — and nothing is waiting on a confirmation."
        )
        raise typer.Exit()
    for f in findings:
        colour = {"confirmed": "green", "rejected": "red"}.get(f.kind, "cyan")
        console.print(f"  [{colour}]{f.kind:9}[/] {f.company} · {f.title[:40]}")
        console.print(f"            [dim]{f.subject[:70]}[/]")


@app.command("resume")
def resume_cmd(
    pdf: str = typer.Argument(..., help="Path to the resume PDF."),
    label: str = typer.Option("", help="Short name; defaults to the filename."),
    roles: str = typer.Option(
        "", help="Comma-separated target roles this resume should win."
    ),
) -> None:
    """Add a resume: copy it into profile/, register it, and verify it.

    The other easy path is Telegram: send the PDF to the bot, optionally
    with a caption like `ml: ML Engineer, Data Scientist`.
    """
    from .wizard import check_summary, register_resume

    source = Path(pdf).expanduser()
    if not source.exists() or source.suffix.lower() != ".pdf":
        console.print(f"[red]Not a PDF I can find: {source}[/]")
        raise typer.Exit(1)
    _profile_or_exit()

    dest = ROOT / "profile" / source.name
    if source.resolve() != dest.resolve():
        shutil.copy(source, dest)
    name = label or source.stem.lower().replace(" ", "_")
    wanted = [part.strip() for part in roles.split(",") if part.strip()]

    if problem := register_resume(f"profile/{dest.name}", name, wanted):
        console.print(f"[red]Not added: {problem}.[/]")
        raise typer.Exit(1)
    console.print(f"[green]Added[/] {dest.name} as “{name}”"
                  + (f" targeting {', '.join(wanted)}" if wanted else ""))
    console.print(check_summary())
    console.print("It joins the scoring from the next batch.")


@app.command("listen")
def listen_cmd(
    minutes: int = typer.Option(0, help="Stop after N minutes; 0 = run forever."),
) -> None:
    """Answer Telegram around the clock.

    Handles taps and typed commands within seconds — decisions, approvals,
    outcome reports, `window 3d`, and form answers — and stands down
    automatically whenever a batch is running. Meant to live under launchd
    (see scripts/jobagent.listen.plist.example).
    """
    from .listen import listen

    profile = _profile_or_exit()
    console.print("Listening… (Ctrl-C to stop)")
    try:
        listen(profile, minutes=minutes,
               on_event=lambda m: console.print(f"  [dim]{m}[/]"))
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("Stopped.")


@app.command("yours")
def yours_cmd(
    remind: bool = typer.Option(
        False, "--remind",
        help="Re-send the Applied/Already applied/Ignored buttons on "
        "Telegram for the oldest ones.",
    ),
) -> None:
    """Jobs you took to apply by hand that have no outcome recorded yet.

    A job leaves this list the moment you tap ✅ Applied, ♻️ Already
    applied, or ✖️ Ignored — or tell the assistant directly.
    """
    from datetime import datetime as _dt

    from . import schedule
    from .notify.telegram import Telegram

    with db.connect() as conn:
        pending = db.handed_off_unreported(conn)
    if not pending:
        console.print("Nothing outstanding — every handed-off job is reported.")
        raise typer.Exit()

    console.print(f"\n[bold]{len(pending)} handed-off, no outcome yet[/]\n")
    for job, handed_at in pending:
        try:
            days = (_dt.now() - _dt.fromisoformat(handed_at)).days
            age = f"{days}d" if days else "today"
        except ValueError:
            age = "?"
        console.print(f"  [dim]{age:>6}[/]  {job.company[:22]:22} {job.title[:44]}")
        console.print(f"          [blue]{job.url}[/]")

    if remind:
        telegram = Telegram.from_env()
        if telegram is None:
            console.print("[red]Telegram is not configured.[/]")
            raise typer.Exit(1)
        for job, _ in pending[:8]:
            schedule.send_outcome_prompt(telegram, job)
        console.print(f"\nSent buttons for the oldest {min(len(pending), 8)}.")


@data_app.command("h1b")
def data_h1b() -> None:
    """Download USCIS H-1B employer data (used by the sponsorship filter)."""
    console.print("Downloading USCIS H-1B Employer Data Hub…")
    for year, status in sponsorship.fetch_h1b_data():
        colour = "green" if "download" in status or status == "cached" else "yellow"
        console.print(f"  {year}: [{colour}]{status}[/]")
    index = sponsorship.load_h1b_index()
    console.print(f"[green]Indexed {len(index):,} employers.[/]")
    if not index:
        console.print(
            "[yellow]No data indexed. Newer years can be downloaded manually "
            f"into {sponsorship.h1b_dir()} — any *.csv with a year in the "
            "filename is picked up.[/]"
        )


@companies_app.command("discover")
def companies_discover(
    extra: list[str] = typer.Argument(None, help="Additional company slugs."),
) -> None:
    """Probe company boards and record which ATS each one uses."""
    slugs = list(dict.fromkeys([*SEED_COMPANIES, *(extra or [])]))
    console.print(f"Probing {len(slugs)} companies across Greenhouse/Lever/Ashby…")

    async def run():
        async with make_client() as client:
            return await discover_many(client, slugs)

    found = asyncio.run(run())
    existing = _load_boards()
    existing.update(found)
    _save_boards(existing)

    by_ats: dict[str, int] = {}
    for ats, _ in found.values():
        by_ats[ats.value] = by_ats.get(ats.value, 0) + 1

    console.print(f"[green]{len(found)}/{len(slugs)} resolved[/] — {by_ats}")
    console.print(f"Saved to {data_dir() / BOARDS_FILE}")


@companies_app.command("list")
def companies_list() -> None:
    """Show the resolved boards."""
    boards = _load_boards()
    if not boards:
        console.print("[yellow]No boards yet. Run: job-agent companies discover[/]")
        raise typer.Exit()
    table = Table("Company", "ATS", "Slug")
    for name, (ats, slug) in sorted(boards.items()):
        table.add_row(name, ats.value, slug)
    console.print(table)
    console.print(f"{len(boards)} boards")


@companies_app.command("workday")
def companies_workday(
    url: str = typer.Argument(
        ...,
        help="A Workday careers URL, e.g. "
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    ),
    slug: str = typer.Option("", help="Name to file it under (defaults to tenant)."),
) -> None:
    """Register a Workday tenant from its careers URL.

    Workday site paths are arbitrary per-company strings, so they cannot be
    guessed — paste the URL from the company's careers page instead.
    """
    from .discover.sources.workday import Board

    board = Board.from_url(url)
    if not board:
        console.print(
            "[red]Not a Workday URL.[/] Expected something like\n"
            "  https://<tenant>.wd5.myworkdayjobs.com/<SiteName>"
        )
        raise typer.Exit(1)

    key = slug or board.tenant

    async def check():
        async with make_client() as client:
            from .discover.sources import workday as wd

            wd.BOARDS[key] = board
            return await wd.probe(client, key)

    if not asyncio.run(check()):
        console.print(
            f"[yellow]Parsed as tenant={board.tenant} shard={board.shard} "
            f"site={board.site}, but the board returned no postings.[/]\n"
            "Double-check the site name in the URL."
        )
        raise typer.Exit(1)

    boards = _load_boards()
    boards[key] = (ATS.WORKDAY, key)
    _save_boards(boards)

    registry = data_dir() / "workday_boards.json"
    existing = json.loads(registry.read_text()) if registry.exists() else {}
    existing[key] = {
        "tenant": board.tenant, "shard": board.shard, "site": board.site
    }
    registry.write_text(json.dumps(existing, indent=2))

    console.print(
        f"[green]Registered[/] {key} → {board.tenant}.{board.shard} / {board.site}"
    )


@companies_app.command("block")
def companies_block(
    name: str, note: str = typer.Option("", help="Why.")
) -> None:
    """Never apply to this company."""
    from .models import normalize_company

    with db.connect() as conn:
        db.set_override(conn, normalize_company(name), Verdict.BLOCK, note)
    console.print(f"[red]Blocked[/] {name}")


@companies_app.command("allow")
def companies_allow(
    name: str, note: str = typer.Option("", help="Why.")
) -> None:
    """Always allow this company, overriding the filters."""
    from .models import normalize_company

    with db.connect() as conn:
        db.set_override(conn, normalize_company(name), Verdict.ALLOW, note)
    console.print(f"[green]Allowed[/] {name}")


@app.command()
def discover(
    limit_boards: int = typer.Option(0, help="Only poll the first N boards."),
) -> None:
    """Pull every board, filter, score, and queue what fits."""
    profile = _profile_or_exit()
    boards = _load_boards()
    if not boards:
        console.print("[yellow]No boards. Run: job-agent companies discover[/]")
        raise typer.Exit(1)
    if limit_boards:
        boards = dict(list(boards.items())[:limit_boards])

    resumes = load_resumes(profile.resumes)
    if not resumes:
        console.print(
            "[yellow]No resumes found — scoring will be titles-only.\n"
            "Add them under `resumes:` in profile/profile.yaml.[/]"
        )

    if not sponsorship.dataset_available():
        console.print(
            "[yellow]H-1B dataset missing; sponsorship unverifiable "
            "(run: job-agent data h1b)[/]"
        )

    console.print(f"Polling {len(boards)} boards…")
    postings = asyncio.run(fetch_all(boards, search=profile.preferences.titles))
    console.print(f"  {len(postings):,} postings fetched")

    jobs = dedupe(postings)
    console.print(f"  {len(jobs):,} after dedupe")

    with db.connect() as conn:
        overrides = db.load_overrides(conn)
        run_id = db.start_run(conn, "discover")

    kept, stats = process(jobs, profile, resumes, overrides)
    stats = persist(kept, stats)

    with db.connect() as conn:
        db.finish_run(
            conn, run_id, found=len(postings), new_jobs=stats.new_jobs,
            blocked=stats.blocked, queued=len(kept),
        )

    console.print(
        f"\n[green]{stats.new_jobs} new[/] · {len(kept)} queued · "
        f"[red]{stats.blocked} blocked[/] · {stats.gated} filtered out"
    )
    if stats.block_reasons:
        console.print("\n[dim]Top filter reasons:[/]")
        for reason, count in sorted(
            stats.block_reasons.items(), key=lambda kv: -kv[1]
        )[:6]:
            console.print(f"  [dim]{count:>4}[/] {reason}")

    console.print("\nNext: [bold]job-agent list[/bold]")


@app.command("list")
def list_jobs(
    limit: int = typer.Option(25, help="How many to show."),
    min_score: float = typer.Option(0.0, help="Minimum match score."),
    show_ask: bool = typer.Option(True, help="Include companies flagged 'ask'."),
    since: str = typer.Option(
        "", "--since", "-s",
        help="Time window: 24h, 3d, 1w, 30d, or 'all'. Default: all.",
    ),
    hours: int = typer.Option(0, "--hours", help="Window in raw hours."),
) -> None:
    """The ranked queue."""
    try:
        window = hours or parse_since(since)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    with db.connect() as conn:
        jobs = db.list_jobs(
            conn, status=JobStatus.NEW, min_score=min_score, limit=limit * 2,
            max_age_hours=window,
        )
        # Show what each window holds, so an empty result is obviously a
        # freshness effect rather than a broken filter.
        windows = {
            lbl: len(db.list_jobs(conn, status=JobStatus.NEW, limit=5000,
                                  min_score=min_score, max_age_hours=h))
            for lbl, h in (("24h", 24), ("3d", 72), ("1w", 168), ("all", 0))
        }

    if not show_ask:
        jobs = [j for j in jobs if j.verdict is Verdict.ALLOW]
    jobs = jobs[:limit]

    if not jobs:
        console.print("[yellow]Queue is empty. Run: job-agent discover[/]")
        raise typer.Exit()

    # Fixed widths that fit an 80-column terminal. Rich shrinks columns to
    # fit the viewport, and it shrinks the leftmost ones first — an overwide
    # table silently erases the score.
    table = Table(show_lines=False, pad_edge=False, box=None)
    table.add_column("#", justify="right", width=3, no_wrap=True)
    table.add_column("%", justify="right", width=3, no_wrap=True)
    table.add_column("Company", width=13, overflow="ellipsis", no_wrap=True)
    table.add_column("Title", width=34, overflow="ellipsis", no_wrap=True)
    table.add_column("Location", width=16, overflow="ellipsis", no_wrap=True)
    table.add_column("", width=1, no_wrap=True)

    for index, job in enumerate(jobs, start=1):
        score = int(job.match_score * 100)
        colour = "green" if score >= 80 else "yellow" if score >= 60 else "dim"
        flag = "[yellow]?[/]" if job.verdict is Verdict.ASK else ""
        table.add_row(
            f"[dim]{index}[/]",
            f"[{colour}]{score}[/]",
            job.company,
            job.title,
            job.location or "—",
            flag,
        )

    console.print(table)
    counts = "  ".join(f"{k} [bold]{v}[/]" for k, v in windows.items())
    console.print(f"[dim]{counts}   ·   --since 24h | 3d | 1w | all[/]")
    console.print("[dim]? = company needs a look (see `job-agent show`)[/]")


@app.command()
def show(rank: int = typer.Argument(1, help="Position in the list.")) -> None:
    """Full detail for one queued job, including why it scored what it did."""
    with db.connect() as conn:
        jobs = db.list_jobs(conn, status=JobStatus.NEW, limit=max(rank, 1))
    if len(jobs) < rank:
        console.print("[yellow]No job at that position.[/]")
        raise typer.Exit(1)

    job = jobs[rank - 1]
    console.print(f"\n[bold]{job.title}[/bold] — {job.company}")
    console.print(f"[dim]{job.location or '—'} · {', '.join(job.sources)}[/]")
    console.print(f"[blue]{job.url}[/]\n")

    console.print(f"Verdict : {job.verdict.value} — {job.verdict_reason or '—'}")

    if b := job.match_breakdown:
        console.print(f"Score   : [bold]{job.match_score:.0%}[/] (resume: {job.best_resume})")
        for label, value, weight in (
            ("skills", b.skills, 0.40),
            ("title", b.title, 0.25),
            ("semantic", b.semantic, 0.25),
            ("recency", b.recency, 0.10),
        ):
            bar = "█" * int(value * 20)
            console.print(f"  {label:<9} {value:5.0%} ×{weight:<5} [dim]{bar}[/]")
        if b.matched_skills:
            console.print(f"  [green]matched[/]  {', '.join(b.matched_skills[:12])}")
        if b.missing_skills:
            console.print(f"  [red]missing[/]  {', '.join(b.missing_skills[:12])}")


@app.command()
def chrome(
    port: int = typer.Option(9222, help="Remote debugging port."),
) -> None:
    """Launch the dedicated Chrome profile used for applying.

    Separate from your everyday Chrome, and required: Chrome 136+ refuses
    remote debugging on the default profile. Log into the job portals once
    here and the sessions persist.
    """
    from .browser import session as bs

    if bs.is_running(port):
        console.print(f"[green]Already running[/] on port {port}.")
        raise typer.Exit()

    if not bs.chrome_binary():
        console.print("[red]Chrome not found.[/]")
        raise typer.Exit(1)

    bs.launch(port)
    ready = asyncio.run(bs.wait_until_ready(port))
    if not ready:
        console.print(f"[red]Chrome did not open a debug port on {port}.[/]")
        raise typer.Exit(1)

    console.print(f"[green]Chrome ready[/] on port {port}")
    console.print(f"  profile: {bs.PROFILE_DIR}")
    console.print(
        "\n[dim]Log into your job portals in this window once — the sessions "
        "persist.\nTip: park it on a second macOS Space so it stays out of "
        "your way.[/]"
    )


@app.command()
def capture(
    name: str = typer.Argument(..., help="Fixture name, e.g. ashby_ramp."),
    url: str = typer.Option("", help="URL to open; omit to use the active tab."),
) -> None:
    """Save the current form as a scrubbed test fixture.

    Greenhouse and Lever can be captured over plain HTTP, but Ashby and
    Workday render client-side and need a real page.
    """
    from .browser import session as bs
    from .forms.capture import save_fixture

    async def run() -> tuple[str, int]:
        sess = await bs.attach(headless=False)
        try:
            page = await sess.open(url) if url else await sess.active_page()
            await page.wait_for_load_state("domcontentloaded")
            html = await page.content()
            return page.url, len(html), html  # type: ignore[return-value]
        finally:
            await sess.close()

    try:
        page_url, _size, html = asyncio.run(run())  # type: ignore[misc]
    except bs.BrowserNotRunning as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    path = save_fixture(name, html)
    from .forms.extract import extract_fields

    count = len(extract_fields(path.read_text()))
    console.print(f"[green]Captured[/] {page_url[:70]}")
    console.print(f"  {path.relative_to(ROOT)} · {path.stat().st_size // 1024} KB "
                  f"· {count} fields · scrubbed")


@app.command()
def fill(
    url: str = typer.Option("", help="Job URL; omit to use the active tab."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be filled, write nothing."
    ),
    resume: str = typer.Option("", help="Resume label to attach."),
) -> None:
    """Fill the application in the active tab. Never submits."""
    from .apply import apply_to_page, unanswered_questions
    from .browser import session as bs

    profile = _profile_or_exit()

    resume_path = None
    if profile.resumes:
        chosen = next(
            (r for r in profile.resumes if r.label == resume), profile.resumes[0]
        )
        resume_path = chosen.path

    async def run():
        sess = await bs.attach(headless=False)
        try:
            page = await sess.open(url) if url else await sess.active_page()
            await page.wait_for_load_state("domcontentloaded")
            return await apply_to_page(page, profile, resume_path, dry_run)
        finally:
            await sess.close()

    try:
        outcome = asyncio.run(run())
    except bs.BrowserNotRunning as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    if outcome.error:
        console.print(f"[red]{outcome.error}[/]")
        raise typer.Exit(1)

    console.print(f"\n[bold]{outcome.url[:76]}[/]")
    console.print(f"[dim]{len(outcome.fields)} fields detected[/]\n")

    table = Table(box=None, pad_edge=False)
    table.add_column("", width=2)
    table.add_column("Field", width=34, overflow="ellipsis", no_wrap=True)
    table.add_column("Value", width=30, overflow="ellipsis", no_wrap=True)
    table.add_column("Via", width=6)

    assert outcome.resolution is not None
    written = {r.ref for r in outcome.fill.written} if outcome.fill else set()
    failed = {r.ref: r.detail for r in outcome.fill.failed} if outcome.fill else {}

    for answer in outcome.resolution.answers:
        if dry_run:
            mark, colour = "·", "dim"
        elif answer.ref in written:
            mark, colour = "✓", "green"
        else:
            mark, colour = "✗", "red"
        table.add_row(
            f"[{colour}]{mark}[/]",
            answer.label,
            answer.value or "[dim](blank)[/]",
            answer.tier.name.lower()[:5],
        )
    console.print(table)

    for ref, detail in failed.items():
        console.print(f"  [red]failed[/] {ref}: {detail}")

    if pending := unanswered_questions(outcome):
        console.print(f"\n[yellow]{len(pending)} need you:[/]")
        for f in pending[:10]:
            flag = "[red]required[/]" if f.required else "[dim]optional[/]"
            console.print(f"  {flag} {f.label[:60]}")

    if outcome.verification and outcome.verification.mismatches:
        console.print("\n[red]Verification mismatches:[/]")
        for m in outcome.verification.mismatches[:8]:
            console.print(f"  {m.label[:40]}: wanted {m.expected!r}, got {m.actual!r}")

    console.print(f"\n{outcome.summary()}")
    if not dry_run:
        gate = (
            "[green]would pass[/]" if outcome.would_auto_submit
            else "[yellow]would NOT pass[/]"
        )
        console.print(f"auto-submit gate: {gate}  [dim](not enabled until Phase 5)[/]")
    console.print("\n[bold]Review the form and submit it yourself.[/]")


@app.command("apply")
def apply_cmd(
    limit: int = typer.Option(3, help="How many applications this run."),
    since: str = typer.Option("", "--since", "-s", help="24h | 3d | 1w | all."),
    min_score: float = typer.Option(-1.0, help="Override the match threshold."),
    submit: str = typer.Option(
        "", help="never | ask | auto. Defaults to profile automation.submit_mode."
    ),
) -> None:
    """Fill queued applications. Submits only per submit_mode."""
    from .run import apply_batch

    profile = _profile_or_exit()
    try:
        window = parse_since(since)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    mode = submit or getattr(profile.automation, "submit_mode", "never")
    if mode not in {"never", "ask", "auto"}:
        console.print(f"[red]submit must be never|ask|auto, got {mode!r}[/]")
        raise typer.Exit(1)

    cap = min(limit, profile.automation.daily_cap)
    console.print(
        f"Applying to up to {cap} jobs · submit_mode=[bold]{mode}[/]"
        + (f" · window {since}" if since else "")
    )

    def on_event(message: str) -> None:
        console.print(f"  [dim]{message}[/]")

    report = asyncio.run(
        apply_batch(
            profile, limit=cap, submit_mode=mode,
            min_score=None if min_score < 0 else min_score,
            max_age_hours=window, on_event=on_event,
        )
    )

    console.print()
    if report.answers_applied:
        console.print(f"[green]{report.answers_applied}[/] Telegram answers applied")
    console.print(
        f"[green]{report.applied} applied[/] · [yellow]{report.stuck} need you[/] · "
        f"{report.pending_approval} awaiting approval · {report.skipped} skipped"
    )
    for err in report.errors[:5]:
        console.print(f"  [red]{err}[/]")


@app.command()
def today(
    mode: str = typer.Option("", help="auto | self. Omit to be asked."),
    since: str = typer.Option("24h", "--since", "-s", help="24h | 3d | 1w | all."),
    limit: int = typer.Option(10, help="How many jobs."),
) -> None:
    """Start the day: apply for you, or hand you the list to do yourself."""
    profile = _profile_or_exit()
    try:
        window = parse_since(since)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    if not mode:
        console.print("\n[bold]Applying today?[/]")
        console.print("  [green]auto[/] — I fill and submit, and message you when stuck")
        console.print("  [cyan]self[/] — I hand you the list, you apply\n")
        mode = typer.prompt("auto or self", default="self").strip().lower()
    if mode not in {"auto", "self"}:
        console.print(f"[red]mode must be auto or self, got {mode!r}[/]")
        raise typer.Exit(1)

    with db.connect() as conn:
        jobs = db.list_jobs(
            conn, status=JobStatus.NEW,
            # self-apply still needs a floor: without one the list fills with
            # 15-30% roles that only look wrong after scoring.
            min_score=(
                profile.preferences.min_match_score if mode == "auto" else 0.50
            ),
            limit=limit, max_age_hours=window,
        )

    if not jobs:
        console.print(f"[yellow]Nothing queued in the last {since}.[/]")
        console.print("Try a wider window:  job-agent today --since 1w")
        raise typer.Exit()

    if mode == "auto":
        from .run import apply_batch
        console.print(f"Auto-applying to {len(jobs)} jobs…")
        report = asyncio.run(
            apply_batch(profile, limit=len(jobs),
                        submit_mode=profile.automation.submit_mode,
                        max_age_hours=window)
        )
        console.print(
            f"\n[green]{report.applied} applied[/] · "
            f"[yellow]{report.stuck} need you[/] · {report.skipped} skipped"
        )
        raise typer.Exit()

    # self-apply: the list, with everything needed to act on it
    console.print(f"\n[bold]{len(jobs)} jobs — last {since}[/]\n")
    lines = []
    for i, job in enumerate(jobs, start=1):
        pct = int(job.match_score * 100)
        colour = "green" if pct >= 75 else "yellow" if pct >= 60 else "dim"
        console.print(
            f"[{colour}]{pct:>3}%[/]  [bold]{job.title[:52]}[/]\n"
            f"      {job.company}  ·  {job.location or '—'}  ·  "
            f"resume: [cyan]{job.best_resume or '—'}[/]\n"
            f"      [blue]{job.url}[/]\n"
        )
        lines.append(f"{pct}%  {job.title}\n{job.company} · resume: {job.best_resume}\n{job.url}")

    tg = None
    try:
        from .notify.telegram import Telegram
        tg = Telegram.from_env()
    except Exception:  # noqa: BLE001
        pass
    if tg:
        tg.send(f"📋 {len(jobs)} jobs to apply to (last {since}):\n\n" + "\n\n".join(lines[:10]))
        console.print("[dim]Same list sent to Telegram.[/]")


@app.command("check")
def check_cmd() -> None:
    """Verify profile.yaml against the resumes it describes.

    The resume is the source of truth: it is what the employer receives, so
    anything the profile asserts that the resume contradicts is a defect in
    the profile.
    """
    from .profile_check import Level, check_all

    profile = _profile_or_exit()
    resumes = load_resumes(profile.resumes, refresh=True)
    if not resumes:
        console.print("[red]No resumes could be parsed — check paths in profile.yaml[/]")
        raise typer.Exit(1)

    results = check_all(profile, resumes)
    errors = warns = 0

    for label, findings in results.items():
        problems = [f for f in findings if f.level is not Level.OK]
        if not problems:
            console.print(f"[green]✓[/] {label}: consistent with the profile")
            continue
        console.print(f"\n[bold]{label}[/]")
        for f in problems:
            if f.level is Level.ERROR:
                errors += 1
                console.print(f"  [red]ERROR[/] {f.field}: {f.detail}")
            else:
                warns += 1
                console.print(f"  [yellow]warn [/] {f.field}: {f.detail}")

    console.print()
    if errors:
        console.print(
            f"[red]{errors} contradiction(s)[/] and {warns} warning(s).\n"
            "Fix profile/profile.yaml before applying — these values go onto "
            "real applications."
        )
        raise typer.Exit(1)
    console.print(f"[green]No contradictions.[/] {warns} warning(s).")


def _discover_now(profile, limit_boards: int = 0) -> int:
    """Poll every board and queue what fits. Returns newly discovered count."""
    boards = _load_boards()
    if not boards:
        return 0
    if limit_boards:
        boards = dict(list(boards.items())[:limit_boards])

    resumes = load_resumes(profile.resumes)
    postings = asyncio.run(fetch_all(boards, search=profile.preferences.titles))

    # The aggregator lane: everything Adzuna indexed in the last 24h for her
    # titles — the "posted in the last 24 hours" board filter, market-wide.
    # Silent without keys; the funnel below treats these like any posting.
    from .config import load_secrets

    secrets = load_secrets()
    if secrets.adzuna_app_id and secrets.adzuna_app_key:
        from .discover.sources import adzuna

        async def _adzuna():
            async with make_client() as client:
                return await adzuna.fetch_fresh(
                    client, secrets.adzuna_app_id, secrets.adzuna_app_key,
                    profile.preferences.titles,
                )
        fresh = asyncio.run(_adzuna())
        console.print(f"  [dim]adzuna: {len(fresh)} fresh posting(s)[/]")
        postings += fresh

    jobs = dedupe(postings)

    with db.connect() as conn:
        overrides = db.load_overrides(conn)
        run_id = db.start_run(conn, "discover")

    kept, stats = process(jobs, profile, resumes, overrides)
    stats = persist(kept, stats)

    with db.connect() as conn:
        db.finish_run(
            conn, run_id, found=len(postings), new_jobs=stats.new_jobs,
            blocked=stats.blocked, queued=len(kept),
        )
    return stats.new_jobs


@app.command()
def batch(
    limit: int = typer.Option(
        0, help="Cap the list at N jobs. 0 = everything queued."
    ),
    since: str = typer.Option(
        "", help="Only jobs posted within this window: 24h, 3d, 1w, 30d, all."
    ),
    repeats: bool = typer.Option(
        False, "--repeats",
        help="Also include jobs already shown in earlier batches.",
    ),
    catch_up: bool = typer.Option(
        False, "--catch-up",
        help="Run only if a scheduled slot was missed (e.g. machine was "
        "off); exit quietly otherwise.",
    ),
    wait: int = typer.Option(90, help="Minutes to wait for your reply."),
    approve_window: int = typer.Option(
        120, help="Minutes to keep listening for `submit <n>` after filling."
    ),
    discover_first: bool = typer.Option(
        True, "--discover/--no-discover", help="Poll the boards first."
    ),
    label: str = typer.Option("", help="Name for this batch. Defaults to the time."),
) -> None:
    """Propose a batch of jobs, ask auto or manual, then fill what you chose.

    This is what the 10am and 3pm schedules run. It never submits — every
    application it fills waits for you to press the button.
    """
    import sys

    from . import propose as propose_mod
    from . import schedule
    from .notify.telegram import Telegram
    from .propose import Mode

    profile = _profile_or_exit()
    telegram = Telegram.from_env()
    interactive = sys.stdin.isatty()

    if catch_up:
        import subprocess

        with db.connect() as conn:
            last = db.last_proposal_started(conn)
        owed = schedule.missed_slot(last)
        if owed is None:
            console.print("[dim]No slot missed — nothing to catch up.[/]")
            raise typer.Exit()
        # Own process matches too, so >1 means another batch is live (e.g.
        # login raced the 10:00 slot) — that one owns the offsets.
        running = subprocess.run(
            ["pgrep", "-f", "job-agent batch"], capture_output=True, text=True
        ).stdout.strip().splitlines()
        if len(running) > 1:
            console.print("[dim]A batch is already running — standing down.[/]")
            raise typer.Exit()
        console.print(f"Catching up the missed {owed:%-I:%M %p} slot…")

    # --since wins; otherwise the standing window she set from Telegram
    # ("window 3d") applies; otherwise no window.
    since = since or schedule.load_window()
    try:
        window = parse_since(since)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    if window:
        console.print(f"[dim]window: last {since}[/]")

    if discover_first:
        console.print("Polling boards…")
        found = _discover_now(profile)
        console.print(f"  [green]{found} new[/]")

    with db.connect() as conn:
        jobs = db.list_jobs(
            conn, status=JobStatus.NEW,
            min_score=profile.preferences.min_propose_score,
            limit=limit or 100_000, max_age_hours=window,
            exclude_proposed=not repeats,
            # By discovery date: "everything that entered the system in the
            # window", so late-surfacing postings are never silently lost.
            age_by="discovered",
        )
        total = db.counts(conn).get("new", 0)

    # Re-gate at proposal time as well as discovery: rows queued before a
    # gate existed (or before the profile tightened) would otherwise keep
    # resurfacing forever from the stored queue.
    from .match.score import passes_gates

    jobs = [j for j in jobs if passes_gates(j, profile)[0]]

    # Aggregator postings carry only a snippet, which starves the skills
    # and semantic components and deflates the total — a strong Annapurna
    # SDE role scored 39% purely for lack of text. When the description is
    # snippet-sized and the TITLE alone is a strong match, the job is
    # proposed despite the floor, labeled provisional in the message.
    if profile.preferences.min_propose_score:
        with db.connect() as conn:
            all_fresh = db.list_jobs(
                conn, status=JobStatus.NEW, min_score=0.0, limit=100_000,
                max_age_hours=window, exclude_proposed=not repeats,
                age_by="discovered",
            )
        seen = {j.dedupe_key for j in jobs}
        from .models import is_workday

        provisional = [
            j for j in all_fresh
            if j.dedupe_key not in seen
            and len(j.description or "") <= 600
            and j.match_breakdown is not None
            # 0.65: "Neuron Runtime Software Development Engineer" scores
            # 0.68 against "Software Engineer" — a 0.7 bar dropped exactly
            # the job this lane exists to catch.
            and j.match_breakdown.title >= 0.65
            # Workday listings carry almost no data; too thin to justify
            # a provisional pass on title fuzz alone.
            and not is_workday(j.ats, j.url)
            and passes_gates(j, profile)[0]
        ]
        jobs += provisional

    # She needs sponsorship, so only proven sponsors are proposed: H-1B
    # filing history or an explicit offer in the JD (verdict ALLOW).
    # "Unproven" companies stay queued rather than being blocked — the
    # dataset ends at 2023 and misses young companies — and are reachable
    # via `job-agent list`, but they don't take up her decision time.
    held_back = 0
    if profile.work_authorization.needs_sponsorship:
        allowed = [j for j in jobs if j.verdict is Verdict.ALLOW]
        held_back = len(jobs) - len(allowed)
        jobs = allowed
    if held_back:
        console.print(
            f"[dim]{held_back} held back — sponsorship unproven. "
            "They show with a ? in: job-agent list[/]"
        )

    if not jobs:
        console.print(
            "[yellow]Nothing new since the last batch.[/]" if not repeats
            else "[yellow]Nothing queued.[/]"
        )
        # Say so on Telegram too. A scheduled run that finds nothing and
        # sends nothing is indistinguishable from a run that never happened
        # — she opened her laptop, saw no 3pm batch, and rightly asked
        # whether the system was broken.
        if telegram and not interactive:
            telegram.send(
                f"🔎 {label or schedule.label_for()} run — nothing new since "
                "the last batch. All quiet; next check at the next slot."
            )
        raise typer.Exit()

    name = label or schedule.label_for()
    run_id, items = schedule.open_batch(jobs, name, total, telegram)

    # Nudge about jobs she took by hand and never reported on — the batch is
    # the moment she is looking at Telegram anyway. Buttons only for the
    # oldest few; the rest are a count, not a flood.
    with db.connect() as conn:
        unreported = db.handed_off_unreported(conn)
    if unreported and telegram:
        telegram.send(
            f"🖐 {len(unreported)} job(s) you took by hand still have no "
            "outcome. Did you apply? Tap below (or the buttons on the "
            "original messages)."
        )
        for job, _ in unreported[:3]:
            schedule.send_outcome_prompt(telegram, job)
    if unreported:
        console.print(f"[dim]{len(unreported)} handed-off unreported — job-agent yours[/]")

    console.print(f"\n[bold]{name} — {len(items)} of {total} queued[/]\n")
    console.print(propose_mod.format_cli(items))
    console.print(
        "\n[dim]Every job waits for your choice — nothing fills unless you "
        "mark it auto.[/]"
    )

    if interactive:
        mode = _ask_mode_cli(run_id, items)
    elif telegram:
        console.print(f"\nAsked on Telegram. Waiting up to {wait} min…")
        mode = schedule.collect(
            run_id, telegram, wait,
            on_event=lambda m: console.print(f"  [dim]{m}[/]"),
            profile=profile,
        )
    else:
        console.print("[red]No terminal and no Telegram — nothing to ask.[/]")
        raise typer.Exit(1)

    result = schedule.BatchResult(run_id=run_id, mode=mode)
    result = schedule.tally(run_id, result)

    if mode is not None and mode is not Mode.MANUAL:
        pairs = schedule.to_fill(run_id, mode)
        console.print(f"\nFilling {len(pairs)}…")
        result.filled = asyncio.run(
            schedule.fill_batch(
                profile, pairs, telegram,
                on_event=lambda *a: console.print(f"  [dim]{a[0]}[/]"),
            )
        )

    console.print("\n" + schedule.summarize(result, telegram))

    # Piggyback the inbox check on the twice-daily schedule: confirmation
    # emails arrive on their own time, and this is the run that's already
    # awake. Configured-but-failing is worth a line; unconfigured is silent.
    from .config import load_secrets

    if load_secrets().gmail_address:
        try:
            from .notify.mail import check as mail_check

            found = mail_check(days=7, telegram=telegram)
            console.print(f"Inbox: {len(found)} application email(s).")
        except Exception as exc:  # noqa: BLE001 - mail must never break a batch
            console.print(f"[yellow]Inbox check failed: {exc}[/]")

    # Each filled form was sent for review. Keep listening, or her
    # `submit 3` lands in an inbox nobody is reading until the next run.
    if result.filled and telegram and not interactive:
        with db.connect() as conn:
            waiting = len(db.open_approvals(conn))
        if waiting:
            console.print(f"\nWatching {approve_window} min for approvals…")
            _watch_approvals(profile, telegram, approve_window)


def _ask_mode_cli(run_id: int, items) -> "object":
    """The same questions, at the terminal."""
    from .propose import Decision, Mode

    answer = typer.prompt("\nauto or manual", default="manual").strip().lower()
    mode = Mode.AUTO if answer.startswith("a") else Mode.MANUAL
    with db.connect() as conn:
        db.set_proposal_mode(conn, run_id, mode.value)
    if mode is Mode.MANUAL:
        return mode

    for item in [i for i in items if i.tier == "ask"]:
        choice = typer.prompt(
            f"  {item.ordinal}. {item.percent}% {item.job.company} · "
            f"{item.job.title[:40]} — auto/manual/ignore",
            default="ignore",
        ).strip().lower()
        decision = {
            "a": Decision.AUTO, "m": Decision.MANUAL, "i": Decision.IGNORE,
            "s": Decision.IGNORE,
        }.get(choice[:1], Decision.IGNORE)
        with db.connect() as conn:
            db.set_decision(conn, run_id, item.ordinal, decision.value)
    return mode


@app.command("submit")
def submit_cmd(
    ordinal: int = typer.Argument(
        0, help="The #number from the review message. Omit to list what's open."
    ),
    all_open: bool = typer.Option(False, "--all", help="Submit every open one."),
    watch: int = typer.Option(
        0, help="Minutes to watch Telegram for `submit <n>` replies instead."
    ),
) -> None:
    """Submit an application you approved.

    The form is filled again and compared against the values you approved.
    If anything differs, nothing is sent and you are told what changed.
    """
    from . import approve
    from .notify.telegram import Telegram

    profile = _profile_or_exit()
    telegram = Telegram.from_env()

    with db.connect() as conn:
        open_rows = db.open_approvals(conn)

    if watch:
        if telegram is None:
            console.print("[red]Telegram is not configured.[/]")
            raise typer.Exit(1)
        _watch_approvals(profile, telegram, watch)
        raise typer.Exit()

    if not ordinal and not all_open:
        if not open_rows:
            console.print("Nothing waiting for approval.")
            raise typer.Exit()
        console.print(f"\n[bold]{len(open_rows)} awaiting your approval[/]\n")
        for row in open_rows:
            with db.connect() as conn:
                job = db.job_by_key(conn, row["dedupe_key"])
            values = approve.loads(row["values_json"])
            console.print(
                f"  #{row['ordinal']}  {job.company if job else '?'} · "
                f"{(job.title if job else '?')[:40]}  "
                f"[dim]{len(values)} fields[/]"
            )
        console.print("\nSubmit one with:  [bold]job-agent submit <n>[/]")
        raise typer.Exit()

    targets = open_rows if all_open else [
        r for r in open_rows if r["ordinal"] == ordinal
    ]
    if not targets:
        console.print(f"[yellow]Nothing open with number {ordinal}.[/]")
        raise typer.Exit(1)

    for row in targets:
        with db.connect() as conn:
            db.set_approval(conn, row["id"], "submit")
        console.print(f"Submitting #{row['ordinal']}…")
        result = asyncio.run(
            approve.submit_approved(
                profile, row, telegram,
                on_event=lambda m: console.print(f"  [dim]{m}[/]"),
            )
        )
        colour = {"submitted": "green", "drift": "yellow", "delta": "yellow"}.get(
            result, "red"
        )
        console.print(f"  [{colour}]{result}[/]")


def _watch_approvals(profile, telegram, minutes: int) -> None:
    """Poll Telegram for `submit <n>` / `skip <n>` and act on them."""
    import time

    from . import approve, schedule

    deadline = time.time() + minutes * 60
    offset = schedule._load_offset()
    console.print(f"Watching for approvals for {minutes} min…")

    while time.time() < deadline:
        texts, offset, callbacks = schedule._fetch_messages(telegram, offset)
        schedule._save_offset(offset)

        for data, query_id in callbacks:
            # Every tap is logged before it is routed. A Submit tap was once
            # swallowed with no trace, and the only way to find out what
            # happened was to guess.
            console.print(f"  [dim]tap: {data[:40]}[/]")
            # A question's choice, or an application-outcome report.
            if schedule.handle_common_callback(telegram, data, query_id):
                continue

            # Tapped a field to change on a review.
            if (field_pick := approve.parse_field_callback(data)) is not None:
                _offer_new_value(telegram, query_id, *field_pick)
                continue

            # Tapped the replacement value for that field.
            if (new_value := approve.parse_value_callback(data)) is not None:
                _apply_edit(profile, telegram, query_id, *new_value)
                continue

            # Tapped Submit/Skip on a review message.
            parsed = approve.parse_review_callback(data)
            if parsed is None:
                telegram.answer_callback(query_id, "Not a review button.")
                continue
            n, choice = parsed
            with db.connect() as conn:
                row = db.approval_by_ordinal(conn, n)
            if row is None:
                telegram.answer_callback(query_id, f"#{n} is already decided.")
                continue

            if choice == "window":
                telegram.answer_callback(query_id, "Opening the window…")
                asyncio.run(schedule.window_session(
                    profile, row["dedupe_key"], telegram, minutes=45))
                continue

            if choice == "edit":
                values = approve.loads(row["values_json"])
                telegram.answer_callback(query_id, "Pick a field to change")
                telegram.send(
                    f"✏️ Which answer should change on #{n}?\n\n"
                    "Tap a field, or reply `edit <number> <new value>`.\n\n"
                    + "\n".join(
                        f"{i}. {v['label'][:38]}: {str(v['value'])[:34]}"
                        for i, v in enumerate(values, start=1)
                    ),
                    buttons=approve.field_buttons(n, values),
                )
                continue
            with db.connect() as conn:
                db.set_approval(conn, row["id"], choice)
                if choice == "skip":
                    db.set_status(conn, row["dedupe_key"], JobStatus.SKIPPED)
            if choice == "skip":
                telegram.answer_callback(query_id, f"Dropped #{n}")
                telegram.send(f"⏭ Dropped #{n}.")
                continue
            telegram.answer_callback(query_id, f"Submitting #{n}…")
            result = asyncio.run(approve.submit_approved(profile, row, telegram))
            console.print(f"  #{n} → {result}")

        for text in texts:
            from . import propose as propose_mod

            if (window := propose_mod.parse_since_command(text)) is not None:
                schedule.apply_since_command(telegram, window)
                continue

            with db.connect() as conn:
                open_rows = db.open_approvals(conn)

            if approve.is_submit_all(text):
                rows = list(open_rows)
            elif (n := approve.parse_submit(text)) is not None:
                rows = [r for r in open_rows if r["ordinal"] == n]
                if not rows:
                    telegram.send(f"⚠️ Nothing open with number {n}.")
            elif (edit := approve.parse_edit(text)) is not None:
                idx, new_value = edit
                with db.connect() as conn:
                    rows_open = db.open_approvals(conn)
                if not rows_open:
                    telegram.send("Nothing is waiting for approval.")
                    continue
                target = rows_open[0]
                vals = approve.loads(target["values_json"])
                if not 1 <= idx <= len(vals):
                    telegram.send(f"No field {idx} on that review.")
                    continue
                _change_answer(profile, telegram, target, idx - 1, new_value)
                continue
            elif (n := approve.parse_skip(text)) is not None:
                for row in [r for r in open_rows if r["ordinal"] == n]:
                    with db.connect() as conn:
                        db.set_approval(conn, row["id"], "skip")
                        # Otherwise the job sits in pending_approval forever:
                        # never submitted, and never offered again either.
                        db.set_status(
                            conn, row["dedupe_key"], JobStatus.SKIPPED
                        )
                    telegram.send(f"⏭ Dropped #{n}.")
                continue
            else:
                continue

            for row in rows:
                with db.connect() as conn:
                    db.set_approval(conn, row["id"], "submit")
                result = asyncio.run(
                    approve.submit_approved(profile, row, telegram)
                )
                console.print(f"  #{row['ordinal']} → {result}")
        time.sleep(schedule.POLL_SECONDS)


async def _read_answer(root, field_) -> str:
    from .fill.verify import read_answer

    return await read_answer(root, field_)


@app.command("with-me")
def with_me_cmd(
    company: str = typer.Argument(..., help="Company name, e.g. plaid."),
    wait: int = typer.Option(45, help="Minutes to leave the form open."),
) -> None:
    """Fill an application and leave it open while you finish it.

    The agent fills what it can, tells you what it could not, and keeps the
    browser on screen. You complete the rest by hand and reply `done`; every
    answer you added is then remembered, so the next form asking the same
    question fills itself.

    The process must stay alive for the window to stay open: Chrome 151
    refuses connect_over_cdp, so Playwright launches the browser itself and
    the browser is a child of this process.
    """
    import time

    from . import approve, schedule
    from .apply import apply_to_page, form_root
    from .browser import session as bs
    from .fill.verify import read_value
    from .notify.telegram import Telegram
    from .resolve import cache as answer_cache
    from .resolve.engine import scope_for
    from .run import _resume_for

    profile = _profile_or_exit()
    telegram = Telegram.from_env()

    with db.connect() as conn:
        row = conn.execute(
            "select dedupe_key from jobs where lower(company) = lower(?) "
            "order by match_score desc limit 1", (company,),
        ).fetchone()
        if row is None:
            console.print(f"[red]No queued job for {company!r}.[/]")
            raise typer.Exit(1)
        job = db.job_by_key(conn, row["dedupe_key"])

    async def run() -> None:
        session = await bs.attach(headless=False)
        try:
            resume = _resume_for(profile, job)
            console.print(f"Opening {job.company} — {job.title[:44]}…")
            page = await session.open(job.url)
            await page.wait_for_timeout(2500)
            outcome = await apply_to_page(
                page, profile, resume.path if resume else None, job=job
            )
            filled = len(outcome.resolution.answers) if outcome.resolution else 0
            blanks = outcome.resolution.unresolved if outcome.resolution else []

            console.print(f"[green]Filled {filled}[/] · {len(blanks)} left for you")
            for f in blanks:
                console.print(f"   {f.type:9} {f.label[:50]}")

            note = (
                f"🖊 {job.title} — {job.company}\n"
                f"Filled {filled} of {filled + len(blanks)}.\n\n"
                + (
                    "Left for you in Chrome:\n"
                    + "\n".join(f"  • {f.label[:52]}" for f in blanks)
                    if blanks
                    else "Nothing left blank."
                )
                + "\n\nFinish them in the open window, then reply `done`.\n"
                "I'll remember whatever you typed."
            )
            if telegram:
                telegram.send(note)
                try:
                    telegram.send_photo(await page.screenshot(full_page=False))
                except Exception:  # noqa: BLE001 - a screenshot is a nicety
                    pass

            # Hold the window open. The browser is a child of this process.
            deadline = time.time() + wait * 60
            offset = schedule._load_offset()

            # Drain whatever is already queued before listening. A `done` sent
            # earlier — for any reason — closed this window the instant it
            # opened, discarding the filled form. Only a reply that arrives
            # after the form is on screen can mean "I have finished it".
            _, offset, _ = schedule._fetch_messages(telegram, offset) if telegram \
                else ([], offset, [])
            schedule._save_offset(offset)
            console.print(f"\n[bold]Window open for {wait} min.[/] Reply `done`.")
            done = False
            while time.time() < deadline and not done:
                if telegram:
                    texts, offset, _ = schedule._fetch_messages(telegram, offset)
                    schedule._save_offset(offset)
                    done = any(schedule.is_done_signal(t) for t in texts)
                done = done or schedule.hand_done_signalled()
                if not done:
                    await asyncio.sleep(10)

            if not done:
                console.print("[yellow]Window closed without a `done`.[/]")

            # Read back whatever is on the form now, hers included.
            root, fields = await form_root(page)
            scope = scope_for(page.url)
            learned = 0
            known = {
                a.label: a.value
                for a in (outcome.resolution.answers if outcome.resolution else [])
            }
            from .resolve.rules import _control_unit

            for field_ in fields:
                if not field_.label or field_.type == "file":
                    continue  # a resume path is not an answer to remember
                if _control_unit(field_):
                    # A date split into month and year selects shares one
                    # label, so learning it caches whichever half was read
                    # last — "Start Date = 2021" then answered the month
                    # control too. The education-date rule already answers
                    # these from the resume; a cache entry only poisons it.
                    continue
                value = await _read_answer(root, field_)
                # Only what she added or changed is worth learning.
                if value and known.get(field_.label) != value:
                    answer_cache.remember(field_.label, value, scope, field_.type)
                    console.print(
                        f"  [green]learned[/] {field_.label[:38]:38} = {value[:32]}"
                    )
                    learned += 1

            console.print(f"\n[green]Remembered {learned}[/] answer(s).")
            if telegram:
                telegram.send(
                    f"✅ Remembered {learned} answer(s) from your edits.\n"
                    "They'll fill themselves on the next form that asks."
                )
        finally:
            await session.close()

    asyncio.run(run())


@app.command("learn")
def learn_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show, don't save."),
) -> None:
    """Read the form open in Chrome and remember what you filled in by hand.

    Run this after finishing an application yourself. Every answer the agent
    could not work out is cached against its question, so the next form asking
    the same thing fills itself.
    """
    from .apply import form_root
    from .browser import session as bs
    from .fill.verify import read_value
    from .forms.extract import extract_fields
    from .resolve import cache as answer_cache
    from .resolve.engine import scope_for

    async def run() -> list[tuple[str, str, str]]:
        session = await bs.attach(headless=False)
        try:
            page = await session.active_page()
            root, fields = await form_root(page)
            scope = scope_for(page.url)
            learned: list[tuple[str, str, str]] = []
            for field_ in fields:
                if not field_.label or field_.type == "file":
                    continue
                value = await _read_answer(root, field_)
                if value:
                    learned.append((field_.label, value, scope))
            return learned
        finally:
            await session.detach()

    try:
        learned = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - report rather than traceback
        console.print(f"[red]Could not read the form:[/] {exc}")
        raise typer.Exit(1) from exc

    if not learned:
        console.print("[yellow]No filled fields found on the open tab.[/]")
        raise typer.Exit()

    console.print(f"\n[bold]{len(learned)} answers on the open form[/]\n")
    saved = 0
    for label, value, scope in learned:
        console.print(f"  {label[:44]:44} = {value[:40]}")
        if not dry_run:
            # remember() already refuses an answer identical to its question.
            answer_cache.remember(label, value, scope, "text")
            saved += 1
    if dry_run:
        console.print("\n[dim]--dry-run: nothing saved.[/]")
    else:
        console.print(f"\n[green]Remembered {saved}[/] for next time.")


@app.command()
def status() -> None:
    """Queue counts and dataset health."""
    with db.connect() as conn:
        by_status = db.counts(conn)
    boards = _load_boards()

    table = Table("", "")
    table.add_row("boards", str(len(boards)))
    table.add_row("H-1B employers", f"{len(sponsorship.load_h1b_index()):,}")
    for name, count in sorted(by_status.items()):
        table.add_row(f"jobs · {name}", str(count))
    console.print(table)


# --------------------------------------------------------------------------
# Tier 3 model choice
# --------------------------------------------------------------------------

llm_app = typer.Typer(help="Which model answers novel questions (Tier 3).")
app.add_typer(llm_app, name="llm")


def _set_env_var(name: str, value: str) -> None:
    """Set NAME=value in .env, replacing an existing line or appending."""
    env_path = ROOT / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    lines = [l for l in lines if not l.strip().startswith(f"{name}=")]
    lines.append(f"{name}={value}")
    env_path.write_text("\n".join(lines) + "\n")


def _print_llm_status() -> None:
    from .resolve import llm

    s = llm.status()
    table = Table("", "")
    table.add_row("provider", str(s["provider"]))
    table.add_row(
        "anthropic",
        ("[green]key set[/]" if s["anthropic_key"] else "[yellow]no ANTHROPIC_API_KEY in .env[/]")
        + f" · model {s['anthropic_model']}",
    )
    table.add_row(
        "qwen (ollama)",
        ("[green]running[/]" if s["ollama_up"] else "[yellow]not running — `ollama serve`[/]")
        + f" · model {s['ollama_model']}",
    )
    table.add_row("tier 3", "[green]available[/]" if llm.available() else "[red]unavailable[/]")
    console.print(table)
    console.print(
        "Switch with `job-agent llm use anthropic|qwen|auto` — "
        "auto tries Claude first, then falls back to Qwen."
    )


@llm_app.callback(invoke_without_command=True)
def llm_main(ctx: typer.Context) -> None:
    """No subcommand: show which model would answer, and why."""
    if ctx.invoked_subcommand is None:
        _print_llm_status()


@llm_app.command("use")
def llm_use(
    provider: str = typer.Argument(..., help="anthropic | qwen | auto"),
) -> None:
    """Choose the Tier 3 provider. Written to .env, so it sticks."""
    from .resolve import llm

    provider = provider.strip().lower()
    if provider not in llm.PROVIDERS:
        console.print(f"[red]Unknown provider {provider!r} — pick one of {', '.join(llm.PROVIDERS)}[/]")
        raise typer.Exit(1)
    _set_env_var("LLM_PROVIDER", provider)
    console.print(f"[green]Tier 3 provider set to {provider}.[/]")
    _print_llm_status()


@llm_app.command("test")
def llm_test() -> None:
    """One tiny round-trip through the configured provider."""
    from .forms.extract import FormField
    from .resolve import llm

    profile = _profile_or_exit()
    fields = [
        FormField(ref="#t1", label="Are you authorized to work in the United States?",
                  type="radio", options=["Yes", "No"]),
        FormField(ref="#t2", label="What is your favorite color?", type="text"),
    ]
    console.print("Asking… (a local model can take a minute)")
    answers, provider = llm.answer_fields(profile, fields, on_event=console.print)
    if not answers and not provider:
        console.print("[red]No provider could answer — run `job-agent llm` to see why.[/]")
        raise typer.Exit(1)
    console.print(f"[green]Answered by {provider}:[/]")
    for f in fields:
        console.print(f"  {f.label} → {answers.get(f.ref) or '[dim](left unanswered — correct!)[/]'}")


if __name__ == "__main__":
    app()
