# Setup Guide

Three parts: install, connect Zoho, test. Works on macOS, Windows and Linux.

You need: [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and logged in with your claude.ai
account, and `git`.

---

## Part 1 — Install

### Step 1 — Install uv and the tool

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

uv tool install git+https://github.com/alanchrissantony/Zoho-Time-Draft.git
```

From a checkout of this repo, `uv tool install -e .` does the same, and your edits apply right away.

### Step 2 — Connect Outlook (for meetings)

Meetings are read through the **Microsoft 365** connector of your claude.ai account. In
claude.ai → Settings → Connectors, connect Microsoft 365 and sign in with your work account.
Claude Code must be logged in with that same claude.ai account (`claude` → `/login`).

The draft job only lets that call use the read-only `outlook_calendar_search` tool. If the
connector isn't connected, drafts are still made from Claude Code activity, with a note saying
meetings weren't read. To turn meetings off, set `TIMELOG_MEETINGS=0` in `~/.timelog/config.env`.

### Step 3 — Install the hook and the background jobs

```bash
timelog install
```

It can be run again safely (do that after every `uv tool upgrade zoho-timelog`). It:
- registers `timelog hook` as a `UserPromptSubmit` hook in `~/.claude/settings.json`, keeping your other
  settings (backed up to `settings.json.bak`). An old `~/.claude/hooks/timelog.sh` from earlier installs
  becomes a forwarder to it, so sessions that were already open keep being recorded
- seeds `~/.timelog/projects.json` (repo → Zoho project mapping), `~/.timelog/config.env` (optional settings)
  and `~/.timelog/days.json` (missed days are checked from 14 days before install)
- schedules two jobs for your user: launchd on macOS, Task Scheduler on Windows, systemd on Linux
  (without systemd, e.g. WSL, it prints the crontab lines to add instead)
  - the **draft job**: at login, 10:00 and 15:00 (catching up after sleep), it makes drafts for finished days
    and notifies you about missed weekdays
  - the **review app** at http://127.0.0.1:8765, started at login and restarted if it stops
- opens the review app, to connect Zoho (Part 2)

`timelog doctor` checks the whole setup and says what to fix. `timelog uninstall` removes the hook and
the jobs (your data in `~/.timelog` stays).

On macOS, `brew install terminal-notifier` makes clicking a notification open the review app.

---

## Part 2 — Connect Zoho

Time is logged as you, through your **own** Zoho API client (a "Self Client"). Nobody else's keys are
involved, and yours stay on your computer (`~/.timelog/zoho_token.json`, readable only by you).

The review app shows a **Connect Zoho** form the first time (or run `timelog login` in a terminal):

1. Open the [Zoho API Console](https://api-console.zoho.in) and choose **Self Client** (create one if
   asked). On its **Client Secret** tab, copy the **Client ID** and **Client Secret** into the form.
2. On the **Generate Code** tab, paste this scope, choose **10 minutes**, type any description and press
   **Create**:
   ```
   ZOHOPEOPLE.timetracker.ALL,ZOHOPEOPLE.leave.READ,AaaServer.profile.Read
   ```
3. Copy the code Zoho shows into the form's **Code** field and press **Connect**.

The app exchanges the code for a long-lived token and reads your Zoho email; the header then shows
"Zoho: you@company.com". **Disconnect** there forgets the connection.

If it goes wrong:
- *"That code has expired or was already used"*: a code works once, within the time you chose. Generate a
  new one.
- *"Zoho doesn't recognise that Client ID"* / *"Client Secret doesn't match"*: copy both again from the
  Client Secret tab.
- *"Zoho didn't share your email"*: type your Zoho email in the form and connect with a new code.
- Your Zoho account isn't on zoho.in: set `ZOHO_DOMAIN=zoho.com` (or `zoho.eu`, `zoho.com.au`, …) in
  `~/.timelog/config.env`, then use that domain's API console.
- Reads fail with `9003 Wrong value given for user parameter`: the email isn't the one on your Zoho People
  profile. Disconnect and connect again, typing the right email.

---

## Part 3 — Test it

```bash
timelog doctor               # every line should say OK
timelog status               # today's capture, drafts to review, missed weekdays
timelog draft 2026-11-04     # a preview of one day's drafts (use today's date)
```

Then open http://127.0.0.1:8765. Nothing is logged to Zoho until you press **Accept** there.

---

## For developers: credentials in `.env`

`ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN` and `ZOHO_USER` (your Zoho email) in the
environment, `~/.timelog/config.env` or a checkout's `.env` take precedence over Connect Zoho. The
easiest way to get a refresh token is `timelog login` once, then copy it from `~/.timelog/zoho_token.json`.
Or exchange a Self Client code directly:

```bash
curl -X POST "https://accounts.zoho.in/oauth/v2/token" \
  -d "code=YOUR_CODE&client_id=YOUR_CLIENT_ID&client_secret=YOUR_CLIENT_SECRET&grant_type=authorization_code"
```
