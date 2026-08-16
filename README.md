# job-agent

Finds jobs that actually fit you, filters out the noise, fills the
applications — and **never presses Submit**. You review every filled form
(screenshot + the exact values, on Telegram or in an open browser window)
and you press the button.

```
discover → score → propose on Telegram → you pick → fill → you review → you submit
```

## What makes it different

- **It never submits on its own.** The scheduled runs hard-code
  `submit_mode="never"`. Even the "submit for me" Telegram button re-fills
  the form at submit time and refuses to send if any value differs from
  what you approved (the drift check). If the form merely *grew* a new
  field, you're shown just the new values and asked again.
- **It never invents answers.** Values come from your `profile.yaml`
  (verified against your actual resume PDFs by `job-agent check`), from
  answers you've given before, or from deterministic rules. An LLM (Tier 3)
  may draft answers for novel questions, but only from your profile facts,
  and its answers always go to your review — they are never auto-submitted
  and never cached.
- **It learns from you, safely.** Every question you answer once — by
  Telegram tap, typed reply, or by hand in the held-open window — becomes a
  cached answer for every later form. The cache refuses known poisoning
  patterns (labels stored as answers, browser artefacts, ambiguous
  split-date labels).
- **Some portals are yours.** Workday requires a per-company account, so
  Workday jobs are listed with their URL for you to apply by hand — the
  agent won't create or hold accounts. LinkedIn Easy Apply is banned by
  default (`never_apply_portals`). Jobs you take by hand get
  Applied / Already applied / Ignored buttons on Telegram, and
  `job-agent yours` tracks the ones you haven't reported on.
- **Only relevant jobs reach you.** A posting must clear a title-relevance
  gate against your wanted titles, a configurable match-score floor, and
  (if you need sponsorship) a proven-H-1B-sponsor check; postings requiring
  citizenship or a security clearance are dropped outright. Batches never
  repeat a job you've already been shown.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium

job-agent init --wizard  # guided: profile, resumes, Telegram bot, keys
job-agent data           # downloads reference datasets (H-1B filings, boards)
```

The wizard asks a few questions, sets up your Telegram bot (it discovers
your chat id for you and sends a test message), and then verifies the
profile against your actual resume PDFs. Prefer files? The manual path is
`cp profile/profile.example.yaml profile/profile.yaml`, `cp .env.example
.env`, edit both, then `job-agent check`.

Filling uses a dedicated Chrome profile (`job-agent chrome` launches it) so
your personal browser is never touched.

## Daily use

```bash
job-agent batch          # propose a batch: ranked list → auto/manual → fill
job-agent list           # the ranked queue
job-agent show <n>       # why a job scored what it did
job-agent with-me <co>   # fill an application, leave the window open for you
job-agent submit --watch 60   # listen for `submit <n>` approvals
job-agent yours          # jobs you took by hand with no outcome reported
job-agent confirm        # scan your inbox for confirmation emails
job-agent status         # queue counts and dataset health
```

`job-agent confirm` (needs a Gmail app password in `.env`) reads your inbox
— strictly read-only — and records the employer's own "thanks for applying"
email on the application: stronger evidence than any submit click. It also
runs automatically at the end of each scheduled batch when configured.

`job-agent batch` is what the schedule runs: it discovers, sends the ranked
list with match percentages to Telegram, waits for your per-job
`auto`/`manual`/`ignore` decisions, fills what you chose, and parks every
filled form for your review. By default it shows everything queued;
`--since 24h|3d|1w` narrows to fresh postings and `--limit N` caps the list.

Optionally set `TELEGRAM_JOBS_CHAT_ID` (e.g. a group you add the bot to) to
send the ranked lists to their own chat, keeping your main chat for form
questions, reviews, and submissions.

To run it twice a day, see `scripts/jobagent.batch.plist.example`
(macOS LaunchAgent; instructions inside).

## Tier 3: the LLM resolver

Questions the cache and rules can't answer can be drafted by a model:
Claude (needs `ANTHROPIC_API_KEY`) with automatic fallback to a local
Ollama model — or local-only, or off. `job-agent llm` shows who would
answer; `job-agent llm use anthropic|qwen|auto|off` switches.

## Your data stays yours

Everything personal lives outside git by design: `profile/` (your resume
PDFs and answers), `data/` (queues, learned answers, logs), `.env`
(credentials), and the Chrome profile are all ignored. `job-agent capture`
scrubs personal values before a form fixture may be saved. A clone of this
repo contains code and fictional test data — nothing about you, and
nothing about the author.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

The suite is pure-logic plus a headless-Chromium widget suite; it never
touches your Chrome profile or any real application.

## License

MIT — see [LICENSE](LICENSE).
