# job-agent

Finds jobs that actually fit you, filters out the noise, autofills the
applications — and **never presses Submit**. You review every autofilled form
(screenshot + the exact values, on Telegram or in an open browser window)
and you press the button.

```
discover → score → propose on Telegram → you pick → autofill → you review → you submit
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

**You need:** Python 3.12+, Google Chrome, a Telegram account, and your
resume as PDF(s). macOS or Linux. On macOS, do **not** put the clone in
`~/Downloads`, `~/Desktop`, or `~/Documents` — scheduled runs are denied
access to those folders; `~/Developer` or `~/code` work fine.

### 1. Install

```bash
git clone https://github.com/Srividya25/job-agent.git
cd job-agent
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium
alias job-agent=$PWD/.venv/bin/job-agent   # or add .venv/bin to PATH
```

### 2. Create your profile (pick one path)

**Guided (recommended):** `job-agent init --wizard` asks about you one
question at a time — identity, work authorization, education, skills,
wanted titles, resume paths — then sets up Telegram (it discovers your
chat id for you and sends a test message), takes your optional API keys,
and finishes by verifying the profile against your actual resume PDFs.

**Manual:** copy the templates and edit them:

```bash
cp profile/profile.example.yaml profile/profile.yaml
cp .env.example .env
```

In `profile.yaml`, every section matters to a different part of the agent:

- `identity` / `address` — filled into application forms verbatim.
- `work_authorization` — `needs_sponsorship: true` turns on the H-1B
  filter: companies without sponsorship history are held back, and
  postings requiring citizenship or a security clearance are dropped.
- `education`, `experience` — must match your resume exactly; the agent
  refuses to apply while they contradict it (see step 3).
- `skills.must_have` — the skills you want to be hired for; 40% of the
  match score.
- `preferences.titles` — the roles you want. Jobs whose titles don't
  relate to these are never shown (`min_title_match`), and jobs scoring
  under `min_propose_score` overall are never proposed.
- `resumes` — one entry per resume version, with `target_roles` so the
  right resume wins the right job. Drop the PDFs in `profile/`.
  Adding one later is one step: `job-agent resume path/to/new.pdf
  --roles "ML Engineer"` — or just send the PDF to your Telegram
  bot, optionally captioned `ml: ML Engineer, Data Scientist`.

Both `profile.yaml` and `.env` are gitignored — nothing personal can end
up in a commit, and a pre-commit hook double-checks that.

### 3. Verify the profile against your resume

```bash
job-agent check
```

This compares every claim in `profile.yaml` (school, degree, email, name,
experience) against the text of your resume PDFs and reports
contradictions. It exists because of a real incident where a stale profile
value put the wrong university into forms; the agent refuses to apply
while an ERROR-level contradiction stands.

### 4. Connect Telegram (the approval channel)

If you used the wizard, this is already done. Manually:

1. In Telegram, message **@BotFather** → `/newbot` → pick any name; copy
   the token it gives you into `TELEGRAM_BOT_TOKEN` in `.env`.
2. Send your new bot any message (it can't message you first).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and
   read `"chat":{"id":…}` — that number is `TELEGRAM_CHAT_ID`.

Everything the agent needs from you arrives here: ranked job lists with
Auto/Manual/Ignore buttons, questions it can't answer, filled-form
screenshots for review, and the Submit button that only you press.

### 5. Download the reference datasets

```bash
job-agent data                 # USCIS H-1B employer data (~84k employers)
job-agent companies discover   # probe which ATS each seed company uses
```

The H-1B data is what backs the sponsorship filter. `companies discover`
builds `data/boards.json` — the boards polled on every run. Add your own
targets: `job-agent companies discover mycompany`, or for Workday tenants
`job-agent companies workday <careers URL>`.

### 6. Optional extras (`.env`)

- `ANTHROPIC_API_KEY` — Claude answers novel form questions and drafts
  short "why us" answers from your profile facts (always shown to you
  before anything is sent, never auto-submitted). Without a key, a local
  [Ollama](https://ollama.com) model is used if one is running; without
  either, novel questions are simply asked to you on Telegram.
- `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` — `job-agent confirm` reads your
  inbox (strictly read-only) and records employers' "thanks for applying"
  emails as submission receipts.
- `ADZUNA_APP_ID/KEY` — extra discovery source.

### 7. First run

```bash
job-agent chrome    # opens the dedicated Chrome profile (sign into nothing,
                    # or into the ATS accounts you already have)
job-agent batch     # discover → score → propose on Telegram
```

On Telegram you'll get the ranked list. Tap **Auto** under a job and it
starts filling immediately; **Manual** marks it yours (with report-back
buttons for later); **Ignore** drops it. Filled applications come back as
a screenshot plus every value, and nothing is submitted until you say so.
Batches never repeat a job you've already been shown.

### 8. Schedule it (optional)

macOS: copy `scripts/jobagent.batch.plist.example` to
`~/Library/LaunchAgents/`, replace the placeholder paths (instructions in
the file), and `launchctl load` it — the agent then proposes twice a day.
A slot missed while the Mac was asleep runs on wake; for slots missed
while it was powered **off**, also install
`scripts/jobagent.catchup.plist.example` — it runs `batch --catch-up` at
login, which fires only when a scheduled slot actually went uncovered.
Linux: an equivalent cron line is
`0 10,15 * * * cd /path/to/job-agent && .venv/bin/job-agent batch`.

### Troubleshooting

- **Scheduled runs die instantly on macOS** — the clone is in
  `~/Downloads`/`~/Desktop`/`~/Documents`; move it (see top) and re-point
  the plist.
- **"Could not open the browser"** — another Chrome is holding
  `chrome-profile/`. Close that window (your personal Chrome is separate
  and unaffected) and retry with `job-agent batch --no-discover`.
- **Bot doesn't react to typed replies in a group** — BotFather →
  Bot Settings → Group Privacy → turn **off**. Button taps work either way.
- **Every company says "no H-1B filing history"** — run `job-agent data`;
  the dataset isn't downloaded yet.
- **Nothing gets proposed** — your gates may be strict; try
  `job-agent list --min-score 0` to see what's queued and loosen
  `min_propose_score` / `min_title_match` in `profile.yaml`.

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
`--since 24h|3d|1w` narrows to fresh postings and `--limit N` caps the
list. You can also just text the bot `window 3d` (or `24h`, `1w`,
`all`) — that becomes the standing window for every future batch.

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
