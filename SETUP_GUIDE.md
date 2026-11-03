# Setup Guide

## Part 1 — Get Your Zoho Account Information

You need 5 values from Zoho. All of them go into your `.env` file.

---

### Step 1 — Get Client ID and Client Secret

1. Go to [https://api-console.zoho.in](https://api-console.zoho.in) and sign in
2. You will see your **Self Client** application listed — click on it
3. Click the **Client Secret** tab
4. Copy your **Client ID** and **Client Secret**

```env
ZOHO_CLIENT_ID=your_client_id_here
ZOHO_CLIENT_SECRET=your_client_secret_here
```

---

### Step 2 — Get Your Refresh Token

The refresh token lets the app log in to Zoho on your behalf automatically.

1. In the same Self Client popup, click the **Generate Code** tab
2. Fill in the following:
   - **Scope:** `ZOHOPEOPLE.timetracker.ALL`
   - **Code expiry duration:** 3 minutes
   - **Description:** anything (e.g. `Zoho timesheet chatbot`)
3. Click **CREATE** — a code will appear in a popup
4. **Immediately** run the command for your OS below (replace `YOUR_CODE`, `YOUR_CLIENT_ID`, `YOUR_CLIENT_SECRET`):

**Linux / macOS (Terminal)**
```bash
curl -X POST "https://accounts.zoho.in/oauth/v2/token" \
  -d "code=YOUR_CODE&client_id=YOUR_CLIENT_ID&client_secret=YOUR_CLIENT_SECRET&grant_type=authorization_code"
```

**Windows — Command Prompt**
```cmd
curl -X POST "https://accounts.zoho.in/oauth/v2/token" -d "code=YOUR_CODE&client_id=YOUR_CLIENT_ID&client_secret=YOUR_CLIENT_SECRET&grant_type=authorization_code"
```

**Windows — PowerShell**
```powershell
Invoke-RestMethod -Method Post -Uri "https://accounts.zoho.in/oauth/v2/token" `
  -Body "code=YOUR_CODE&client_id=YOUR_CLIENT_ID&client_secret=YOUR_CLIENT_SECRET&grant_type=authorization_code"
```

5. The response will look like this:

```json
{
  "access_token": "...",
  "refresh_token": "1000.xxxxxx.xxxxxx",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

6. Copy the `refresh_token` value

```env
ZOHO_REFRESH_TOKEN=your_refresh_token_here
```

> **Note:** The code expires in 3 minutes. If you see `{"error":"invalid_code"}`, go back to Step 1 of this section and generate a new code.

---

### Step 3 — Set Your Domain

If your Zoho account is in India (zoho.in), use:
```env
ZOHO_DOMAIN=zoho.in
```

For other regions use `zoho.com`, `zoho.eu`, `zoho.com.au`, etc.

---

### Step 4 — Set Your Email

```env
ZOHO_USER=your_email@company.com
```

This must be exactly the email on your Zoho People profile. If it's wrong, Zoho rejects reads with
`9003 Wrong value given for user parameter`, and the duplicate checks can't see your existing logs.

---

### Step 5 — Verify

From the repo root, with the venv set up (Part 2, Step 1):

```bash
.venv/bin/python -c "
import datetime, zoho_api as z
print('projects:', len(z.catalog()))
logs = z.fetch_existing_logs(datetime.date.today(), z.token(), strict=True)
print('today\'s logs:', len(logs))
"
```

"Zoho auth failed" means a credential problem; a `ZohoAPIError` means a `ZOHO_USER` problem.

---

### Final `.env` file

```env
ZOHO_CLIENT_ID=1000.XXXXXXXXXXXXXXXXXXXX
ZOHO_CLIENT_SECRET=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
ZOHO_REFRESH_TOKEN=1000.XXXXXXXXXXXXXXXX.XXXXXXXXXXXXXXXX
ZOHO_DOMAIN=zoho.in
ZOHO_USER=your_email@company.com
```

---

## Part 2 — Install for Claude Code

### Step 1 — Install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Step 2 — Connect Outlook (for meetings)

Meetings are read through the **Microsoft 365** connector of your claude.ai account. In
claude.ai → Settings → Connectors, connect Microsoft 365 and sign in with your work account.
The Claude Code CLI must be logged in with that same claude.ai account (`claude` → `/login`).

The draft job only lets that call use the read-only `outlook_calendar_search` tool. If the
connector isn't connected, drafts are still made from Claude Code activity, with a note saying
meetings weren't read.

### Step 3 — Install the hook, daily job and review app

```bash
.venv/bin/python timelog.py install
brew install terminal-notifier   # optional: clicking a notification opens the review app
```

This command can be run again safely. It:
- links `hooks/timelog.sh` to `~/.claude/hooks/` and registers it as a `UserPromptSubmit` hook in `~/.claude/settings.json`, keeping your other settings
- unregisters the `zoho-timesheet` chat MCP server if an older install added it
- seeds `~/.timelog/projects.json` from `projects.example.json`, and `~/.timelog/days.json` (missed days are checked from 14 days before install)
- loads the launchd job `com.codemonk.timelog`, which makes drafts for finished days and checks for missed weekdays at login, 10:00 and 15:00 (on wake if the Mac slept through a slot)
- loads the launchd job `com.codemonk.timelog.app`, which keeps the review app running at http://127.0.0.1:8765

### Step 4 — Test it

Check the capture and make a preview of today's drafts:
```bash
.venv/bin/python timelog.py status
.venv/bin/python timelog.py draft $(date +%F)
open http://127.0.0.1:8765
```
Nothing is logged to Zoho until you press **Accept** in the app.
