# Zoho People Time Logs: drafts you review

Every morning this turns yesterday's Claude Code activity and Outlook meetings into **draft**
Zoho People time logs. You accept, edit or decline them in a small local app, and only accepted
drafts are logged. If a weekday has nothing in Zoho, you get a macOS notification.

```
Claude Code prompts ── UserPromptSubmit hook (hooks/timelog.sh) ──► ~/.timelog/YYYY-MM-DD.jsonl
Outlook calendar ───── claude.ai Microsoft 365 connector (read-only) ┐
                                                                     ▼
launchd: at login, 10:00, 15:00 ── timelog.py draft  (finished days only)
   ├─ minute timeline: meetings first, then Claude Code work blocks (no double counting)
   ├─ repo / meeting → Zoho project via ~/.timelog/projects.json
   ├─ claude -p, no tools: picks the job, writes a one-line description, suggests projects
   ├─ compares with what's already in Zoho
   ├─ ~/.timelog/drafts/DATE.json          (nothing is sent to Zoho)
   └─ missed weekdays / drafts to review ──► notification ──► review app

review app  http://127.0.0.1:8765   Accept ─► Zoho People API      Edit · Decline · Add · Mark leave
```

---

## Setup

Prerequisites: Python 3.12+, `jq`, `git`, the Claude Code CLI logged in with your claude.ai
account, and the **Microsoft 365** connector connected in claude.ai (for meetings).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in Zoho credentials (see SETUP_GUIDE.md)
.venv/bin/python timelog.py install
brew install terminal-notifier  # optional: clicking a notification opens the app
```

`install` registers the capture hook, seeds `~/.timelog/projects.json`, and loads two launchd
jobs: the morning draft job and the review app. It also unregisters the `zoho-timesheet` chat
MCP server left by older installs. You can run it again safely.

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
every other setting and path, are in [constants.py](constants.py).

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
.venv/bin/python timelog.py status                 # today's capture, jobs, drafts to review, missed days
.venv/bin/python timelog.py calc 2026-10-28        # a day's timeline (local only, no meetings)
.venv/bin/python timelog.py draft 2026-10-28       # (re)make one day's drafts
.venv/bin/python timelog.py draft                  # what launchd runs: finished days + missed-day check
.venv/bin/python timelog.py accept 2026-10-28 --all   # log a day's drafts without the app
.venv/bin/python timelog.py app                    # run the review app in the foreground
```

### When drafts get made

| Mac state | Yesterday's drafts are made… |
|---|---|
| On at 10:00 | at 10:00 |
| Asleep at 10:00 | when it wakes |
| Off overnight | at your next login |

The 15:00 run reminds you again if something is still missed or waiting. Drafts are made for the
last 7 days from install on (weekends only if you used Claude Code). Missed weekdays are checked
for the last 14 days. Work after midnight counts toward the next day.

Draft runs and every accept are logged in `~/.timelog/run.log`.

---

## Privacy and permissions

- Zoho credentials live only in `.env` (gitignored) and are read by the repo's code. They never
  reach the review page or Claude.
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
- If the `claude` login, the Microsoft 365 connector or the Zoho refresh token expires, the
  day's drafts get a note explaining what's missing; regenerate once it's fixed.

---

## Project structure

| File | Purpose |
|---|---|
| `timelog.py` | CLI: `calc`, `draft`, `accept`, `app`, `install`, `status` |
| `constants.py` | Settings, paths and limits used everywhere; loads `.env` |
| `drafts.py` | Timeline, draft store, generate, accept, missed days, notifications |
| `sources.py` | Activity sources: Claude Code prompts, Outlook meetings, git; headless Claude |
| `review_app.py` + `web/index.html` | Review app |
| `hooks/timelog.sh` | Claude Code `UserPromptSubmit` capture hook |
| `prompts/nightly.md` | Instructions for the job choice, descriptions and suggestions |
| `projects.example.json` | Starting repo / meeting → Zoho mapping |
| `launchd/*.plist` | launchd templates: daily draft job, review app |
| `zoho_api.py` | The one Zoho layer: credentials, cached auth, reads, open-jobs catalog, add a log, time helpers |
| `tests/` | `python -m unittest discover tests` |
