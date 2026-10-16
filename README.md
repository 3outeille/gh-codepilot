# GitHub Activity Dashboard

A self-hosted PWA that monitors GitHub PR/issue activity across multiple repos, sends desktop notifications, and provides an integrated Claude Code terminal for interactive AI-assisted code review.

![Dashboard — PR tab with CI badges, review status, and activity](screenshots/dashboard.png)

![Expanded PR details with rendered markdown comments](screenshots/details.png)

![Claude Code terminal with session history sidebar](screenshots/terminal.png)

## Features

- **PR & Issue Tracking** — All open PRs and issues across watched repos, sorted by recent activity
- **Activity Feed** — Comments, reviews, commits shown under each item with correct timestamps
- **Desktop Notifications** — Native alerts for new PRs, issues, and comments (Linux + macOS)
- **Browser Notifications** — PWA push notifications with click-to-open
- **CI & Review Status** — Green/red/yellow CI badges, review decision badges, draft indicators
- **Inline Diff Viewer** — View PR diffs without leaving the dashboard
- **Detail Viewer** — Expand any item to see the full description, comments, and reviews rendered as markdown
- **Mute/Archive** — Hide noisy items, toggle visibility
- **Filters** — Filter by repo, review status (needs review, approved, changes requested, draft)
- **Claude Code Terminal** — Launch an interactive Claude Code session scoped to any PR or issue
- **Session History** — View and resume past Claude Code sessions per repo/PR
- **Tests tab** — pytest in the PR's own worktree (seconds), on a warm GPU box, or as a one-shot HF Job
- **Bench tab** — benchmarks on HF Jobs, reviewed before they run, charted after, re-runnable byte for byte

## Setup

### Prerequisites

- Python 3.10+
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`) installed
- Linux or macOS

### Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/hraju115/gh-codepilot/main/install.sh | bash
```

This will:
- Clone the repo to `~/.gh-codepilot`
- Create a Python venv and install dependencies
- Set up a cron job for polling (every 10 minutes)
- Start a background service (systemd on Linux, launchd on macOS)

Then edit `~/.gh-codepilot/repos.conf` to add your repos and visit **http://127.0.0.1:5050**.

### Manual Install

```bash
git clone https://github.com/hraju115/gh-codepilot.git
cd gh-codepilot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp repos.conf.example repos.conf
# Edit repos.conf — add your repos, one per line
python app.py
```

### Install as PWA

Open `http://127.0.0.1:5050` in Chrome/Edge and click the install icon in the address bar.

### Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/hraju115/gh-codepilot/main/uninstall.sh | bash
```

## Architecture

```
check_notifications.py (cron, every 10min)
  ├─ gh pr/issue list     → open_items.json (all open PRs/issues)
  ├─ /repos/{repo}/events → events.jsonl (activity log)
  └─ notify-send          → desktop notifications

app.py (Flask + SocketIO, port 5050)
  ├─ GET /                → dashboard (index.html)
  ├─ GET /api/events      → polling endpoint
  ├─ GET /api/detail      → PR/issue body + comments
  ├─ GET /api/diff        → PR diff
  ├─ GET /api/sessions    → Claude Code session history
  ├─ POST /api/mute       → mute an item
  ├─ GET /terminal        → Claude Code terminal page
  ├─ /api/tests/local     → pytest in the PR worktree
  ├─ /api/bench/*         → benchmark runs on HF Jobs
  └─ WebSocket            → PTY I/O for Claude Code
```

## Benchmarks

The **Bench** tab runs benchmarks on HF Jobs. (Tests live in their own tab —
they are pass/fail and cheap, and share almost no controls with this.) A
benchmark is usually written by Claude rather than by hand, so the tab is built
around two questions: *was this designed correctly?* and *can I get exactly
these numbers again?*

**Before it runs.** Point the form at a script and it is reviewed on the spot —
the hardware it will take, the commit it is pinned to, and two separate blocks:
what the script *declares* about itself, and what *reading its source* shows.
The second block is the one that matters. It looks for a warmup, a repetition
count, `torch.cuda.synchronize` around GPU timing, a monotonic timer, a seed,
and an `allclose` between the implementations being compared. A script is free
to claim a warmup it never performs; only the audit can contradict it.

**After it runs.** A run keeps its *recipe* — script source, dependencies,
image, hardware, PR commit, env — hashed to a short id. Two runs sharing that
id are the same experiment, so the header can say "ran 3× identically", and
**Re-run exactly** re-submits the same bytes.

A run has four tabs. **Setup** answers "how was this defined" in one scroll:
the declared plan, the source audit, the hardware and environment, the exact
`hf jobs` command, the script as submitted, and every package version resolved
inside the job. **Plots**, **Metrics** and **Log** are the results. A finished
run cannot change, so moving between its tabs costs no network request; the
job listing is cached briefly and a terminal job's log is cached for good.

**Writing one.** See `examples/benchmark_example.py`. Three conventions:

| Convention | Effect |
|---|---|
| `BENCH_PLAN = {...}` near the top | shown as the declared design (question, arms, sweeps, metrics, method, expectations) |
| one JSON object between bare `JSON_BEGIN` / `JSON_END` lines | every list-of-dicts value becomes a chart — repeated columns read as swept parameters, near-unique ones as measurements |
| `plt.savefig("name.png")` with a relative path | collected from the run's working directory and uploaded to a private Hub dataset, then shown in the gallery |

Charts are drawn in the browser from the JSON, so the numbers and the setup
that produced them live in the same immutable job log and cannot drift apart.
Uploaded images are proxied through the app, so a private results dataset needs
no token in the browser.

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask web app with SocketIO |
| `check_notifications.py` | Cron script — polls GitHub, sends notifications |
| `repo_manager.py` | Git clone/fetch/worktree management |
| `session_reader.py` | Reads Claude Code session history |
| `bench_runner.py` | Benchmark recipes, submitted wrapper, log parsing, design audit |
| `local_runner.py` | Local pytest runs in a per-PR venv |
| `examples/benchmark_example.py` | Worked example of the benchmark contract |
| `pty_manager.py` | PTY lifecycle for Claude Code terminals |
| `repos.conf` | List of repos to watch |
| `templates/index.html` | Dashboard UI |
| `templates/terminal.html` | Claude Code terminal page |
| `static/` | PWA manifest, service worker, icons |
