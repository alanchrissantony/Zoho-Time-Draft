# Zoho People Time Logs: drafts you review

[![CI](https://github.com/alanchrissantony/Zoho-Time-Draft/actions/workflows/ci.yml/badge.svg)](https://github.com/alanchrissantony/Zoho-Time-Draft/actions/workflows/ci.yml)

Every morning this turns yesterday's Claude Code activity and Outlook meetings into **draft**
Zoho People time logs. You accept, edit or decline them in a small local app, and only accepted
drafts are logged. If a weekday has nothing in Zoho, you get a desktop notification.

```
Claude Code prompts ── UserPromptSubmit hook (timelog hook) ─────► ~/.timelog/YYYY-MM-DD.jsonl
Outlook calendar ───── claude.ai Microsoft 365 connector (read-only) ┐
                                                                     ▼
scheduled: at login, 10:00, 15:00 ── timelog draft  (finished days only)
   ├─ minute timeline: meetings first, then Claude Code work blocks (no double counting)
   ├─ repo / meeting → Zoho project via ~/.timelog/projects.json
   ├─ claude -p, no tools: picks the job, writes a one-line description, suggests projects
   ├─ compares with what's already in Zoho
   ├─ ~/.timelog/drafts/DATE.json          (nothing is sent to Zoho)
   └─ missed weekdays / drafts to review ──► notification ──► review app

review app  http://127.0.0.1:8765   Accept ─► Zoho People API      Edit · Decline · Add · Mark leave
```

---

## Install

Works on **macOS, Windows and Linux**. You need [Claude Code](https://docs.claude.com/en/docs/claude-code)
logged in with your claude.ai account, `git`, and (for meetings) the **Microsoft 365** connector
connected in claude.ai → Settings → Connectors. Python comes with uv; you don't install it yourself.

**1. Install uv** (once):

| macOS / Linux | Windows (PowerShell) |
|---|---|
| `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` |

Open a new terminal afterwards so `uv` and the tools it installs (in `~/.local/bin`) are on your PATH.
If `timelog` isn't found later, run `uv tool update-shell` and open a new terminal.

**2. Install the tool and set it up:**

```bash
uv tool install git+https://github.com/alanchrissantony/Zoho-Time-Draft.git
timelog install
```

`timelog install` registers the Claude Code hook, creates `~/.timelog`, schedules the two background
jobs (the draft job and the review app) and opens the review app.

**3. Connect Zoho** in the page that opens, with your own Zoho Self Client: about two minutes, every
step is on the page (or run `timelog login`). Details and troubleshooting: [SETUP_GUIDE.md](SETUP_GUIDE.md).

**4. Check:** `timelog doctor` should say OK on every line.

| | macOS | Windows | Linux |
|---|---|---|---|
| Background jobs | launchd agents `com.codemonk.timelog`, `com.codemonk.timelog.app` | Task Scheduler tasks `codemonk-timelog-draft`, `codemonk-timelog-app` (your user, no admin) | systemd user units `timelog-draft.timer`, `timelog-app.service` |
| Your data and settings | `~/.timelog/` | `%USERPROFILE%\.timelog\` | `~/.timelog/` |
| Notifications | Notification Center (`brew install terminal-notifier` makes a click open the app) | a toast; clicking it opens the app | `notify-send`, if your desktop has it |

On Linux without systemd (WSL, containers) `timelog install` prints the crontab lines to add instead.

### Update

```bash
uv tool upgrade zoho-timelog
timelog install        # safe to re-run; picks up any change to the hook or jobs
```

### Uninstall

```bash
timelog uninstall               # removes the hook and the jobs; keeps ~/.timelog
uv tool uninstall zoho-timelog
```

Delete `~/.timelog` as well if you want your drafts, logs and Zoho connection gone.

---

## Daily use

1. Work as usual. Claude Code prompts are recorded; meetings are read from your calendar.
2. The next morning (10:00, or at login / on wake) drafts are made for the finished day. If a
   weekday has nothing in Zoho, or drafts are waiting, you get a notification.
3. Open **http://127.0.0.1:8765** (or click the notification). For each draft:
   - **Accept** logs it to Zoho. If Zoho already has part of that job's time for the day, only
     the difference is logged ("Accept (log 01:00)").
   - **Edit** changes the project, job, hours or description.
   - **Decline** drops it (Undo brings it back).
4. Day buttons: **Accept all**, **Regenerate** (also works for today as a preview), **Add entry**
   for time nothing captured, and **Mark day as leave / holiday** so it stops counting as missed.
   Full-day leave you've booked and your holidays in Zoho are picked up by themselves ("Leave in Zoho");
   half days aren't, as there's still time to log.

"Not counted" lists what was left out (personal events like farewells, meetings Claude couldn't
place, ignored repos); **Count this** turns one into an entry. "Already in Zoho" shows that day's
existing logs.

Nothing reaches Zoho without your Accept. Regenerating keeps everything you've accepted,
declined, edited or added.

---

## How time is counted

- **Meetings** count for their calendar time. All-day, cancelled and "free" events are skipped;
  tentative ones count (decline them in the app if you didn't attend).
- **Claude Code**: a gap of **more than 30 min** between prompts ends a work block; each block
  counts as last prompt − first prompt **+ 15 min**.
- Each minute belongs to one thing only: meetings win over Claude Code blocks.
- Each draft's total is rounded to the nearest **15 min**; drafts that round to 0 go to "Not counted".

Repos and meetings that map to the same Zoho job are combined into one draft. These numbers, and
every other setting and path, are in [constants.py](zoho_timelog/constants.py).

---

## Mapping to Zoho: `~/.timelog/projects.json`

```json
{
  "groups":   { "codemonk/kawada": { "project": "[Project] Kawada - AI RAG Feature Enhancements", "job": "auto" } },
  "repos":    { "bruno": { "project": "Codemonk Internal Work", "job": "Codemonk Internal Projects (...)" } },
  "ignore":   ["dotfiles"],
  "meetings": [ { "match": "TATA Power Image Analytics", "project": "[Project] Tata Power Image Analytics For Site Safety", "job": "auto" } ],
  "ignore_meetings": ["Farewell", "Birthday"]
}
```

- **`groups`** match the GitLab/GitHub group of `origin`, so one rule covers every repo in a group.
  The longest prefix wins. **`repos`** match the repo folder name and win over groups.
- **`meetings`** match a case-insensitive part of the meeting subject (first match wins);
  **`ignore_meetings`** are never counted.
- **`job: "auto"`** means Claude picks the job from the project's live Zoho job list, using
  branches, commits, prompts and meeting subjects (e.g. `Development - Oct`, `Daily Standup`).
  If none clearly fits, the draft says "Pick a job" and you choose it in the app.
- Unmapped repos and meetings still become drafts when Claude can suggest a project; they carry
  a **suggested** badge so you check them.
- Project and job names must match Zoho exactly; copy them from the dropdowns in the app.

---

## Commands

```bash
timelog status                    # today's capture, jobs, drafts to review, missed days
timelog doctor                    # check the setup; says what to fix
timelog login                     # connect Zoho from the terminal (same as the app's form)
timelog calc 2026-10-28           # a day's timeline (local only, no meetings)
timelog draft 2026-10-28          # (re)make one day's drafts
timelog draft                     # what the scheduled job runs: finished days + missed-day check
timelog accept 2026-10-28 --all   # log a day's drafts without the app
timelog app                       # run the review app in the foreground
timelog install / uninstall       # set up or remove the hook and the jobs
```

### When drafts get made

| Computer at 10:00 | Yesterday's drafts are made… |
|---|---|
| On at 10:00 | at 10:00 |
| Asleep at 10:00 | when it wakes |
| Off overnight | at your next login |

The 15:00 run reminds you again if something is still missed or waiting. Drafts are made for the
last 7 days from install on (weekends only if you used Claude Code). Missed weekdays are checked
for the last 14 days. Work after midnight counts toward the next day.

Draft runs and every accept are logged in `~/.timelog/run.log`; the jobs' own output goes to
`~/.timelog/scheduler.log` and `~/.timelog/app.log`.

### Settings: `~/.timelog/config.env`

Optional; `timelog install` creates it with every option commented out. The common ones:
`TIMELOG_MEETINGS=0` (don't read Outlook), `TIMELOG_MODEL` / `TIMELOG_MEETINGS_MODEL` (Claude models),
`ZOHO_DOMAIN` (if your Zoho account isn't on zoho.in), `TIMELOG_PORT` (if 8765 is taken).
Restart the app afterwards (`timelog install` does it).

---

## Privacy and permissions

- You connect Zoho with your own Self Client. Its Client ID, Client Secret and refresh token are
  saved only in `~/.timelog/zoho_token.json` (readable by you alone) and used only by this tool's
  code. The review page sends them once, to save them, and never gets them back; Claude never sees them.
- The review app listens on 127.0.0.1 only, rejects other `Host` headers, and requires an
  `X-Timelog` header on every change, so other websites can't drive it.
- Prompt text stays in `~/.timelog`. The draft step's `claude -p` call sees prompt excerpts,
  commits and meeting subjects to write descriptions; it runs with **no tools and no MCP servers**.
- The meetings step's `claude -p` call may use only the read-only `outlook_calendar_search` tool.
  Your other connectors and Microsoft 365's other tools aren't even loaded into it, which also
  keeps the call small (about 20k tokens instead of about 115k).
  Only subject, time and status are kept, never bodies or join links.
- Only the descriptions you accept go to Zoho.

## Known limitations

- Only Claude Code and calendar meetings are seen. Add other work with **Add entry**.
- A meeting counts when it's on your calendar, whether or not you joined. Decline the ones you skipped.
- A session that runs past midnight is split by date.
- If the `claude` login, the Microsoft 365 connector or your Zoho connection stops working, the
  day's drafts get a note explaining what's missing; `timelog doctor` says what to fix. Regenerate
  the day once it's fixed.

---

## Project structure

| File | Purpose |
|---|---|
| `pyproject.toml` | Package `zoho-timelog`; the `timelog` command |
| `zoho_timelog/__main__.py` | Entry point; runs `hook` before loading anything heavy |
| `zoho_timelog/cli.py` | CLI: `install`, `uninstall`, `doctor`, `login`, `status`, `calc`, `draft`, `accept`, `app`, `hook` |
| `zoho_timelog/hook.py` | Claude Code `UserPromptSubmit` capture hook (`timelog hook`) |
| `zoho_timelog/constants.py` | Settings, paths and limits; loads `~/.timelog/config.env` (and `.env` in a checkout) |
| `zoho_timelog/oscompat.py` | What differs by OS: subprocesses, file lock, notifications |
| `zoho_timelog/drafts.py` | Timeline, draft store, generate, accept, missed days, notifications |
| `zoho_timelog/sources.py` | Activity sources: Claude Code prompts, Outlook meetings, git; headless Claude |
| `zoho_timelog/review_app.py` + `web/index.html` | Review app |
| `zoho_timelog/zoho_api.py` | The one Zoho layer: your Self Client connection, cached auth, reads, open-jobs catalog, add a log, time helpers |
| `zoho_timelog/prompts/nightly.md` | Instructions for the job choice, descriptions and suggestions |
| `zoho_timelog/projects.example.json`, `config.example.env` | Seeds for `~/.timelog/projects.json` and `config.env` |
| `zoho_timelog/scheduler.py` | Scheduled jobs: launchd (macOS), Task Scheduler (Windows), systemd (Linux) |
| `tests/` | `uv run python -m unittest discover tests` |

---

## Development

```bash
git clone https://github.com/alanchrissantony/Zoho-Time-Draft.git
cd Zoho-Time-Draft
uv tool install -e .                               # `timelog` runs this checkout; edits apply right away
timelog install
uv run --group dev python -m unittest discover tests   # the dev group adds httpx for the route tests
```

In a checkout, `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN` and `ZOHO_USER` in a `.env`
at the repo root take precedence over Connect Zoho (see the end of [SETUP_GUIDE.md](SETUP_GUIDE.md)).
Restart the review app after changing its code: `timelog install`, or `launchctl kickstart -k
gui/$(id -u)/com.codemonk.timelog.app` on macOS.
