"""Settings, paths and limits shared by every module. Importing this loads the config files.

Values marked (env) can be set, highest precedence first, in the process environment,
~/.timelog/config.env, or (in a dev checkout only) the repo's .env. load_dotenv never
overrides a value that is already set, so loading in that order gives that precedence.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PKG_DIR  = Path(__file__).resolve().parent   # installed package: web/, prompts/, the example mapping
REPO_DIR = PKG_DIR.parent                    # the checkout when running from source
DEV_CHECKOUT = (REPO_DIR / "pyproject.toml").exists()

# TIMELOG_DIR comes from the process environment only: config.env lives inside it.
TIMELOG_DIR = Path(os.getenv("TIMELOG_DIR", "~/.timelog")).expanduser()
CONFIG_FILE = TIMELOG_DIR / "config.env"
# Absolute paths, not the cwd: launchd and the other schedulers start us elsewhere.
load_dotenv(CONFIG_FILE)
if DEV_CHECKOUT:
    load_dotenv(REPO_DIR / ".env")

# ── Paths ──────────────────────────────────────────────────────────────────────
PROJECTS_FILE = TIMELOG_DIR / "projects.json"
DRAFTS_DIR    = TIMELOG_DIR / "drafts"
DAYS_FILE     = TIMELOG_DIR / "days.json"
RUN_LOG       = TIMELOG_DIR / "run.log"
GEN_LOCK      = TIMELOG_DIR / ".lock"
SCHED_LOG     = TIMELOG_DIR / "scheduler.log"   # the draft job's output, on every OS
APP_LOG       = TIMELOG_DIR / "app.log"
ZOHO_LOG_FILE = Path(os.getenv("LOG_FILE", TIMELOG_DIR / "zoho_submissions.log")).expanduser()  # (env)
PROMPT_FILE   = PKG_DIR / "prompts" / "nightly.md"
INDEX_HTML    = PKG_DIR / "web" / "index.html"
EXAMPLE_PROJECTS = PKG_DIR / "projects.example.json"
EXAMPLE_CONFIG   = PKG_DIR / "config.example.env"

# ── Review app ─────────────────────────────────────────────────────────────────
HOST    = "127.0.0.1"   # the app only ever listens here
PORT    = int(os.getenv("TIMELOG_PORT", "8765"))   # (env)
APP_URL = f"http://{HOST}:{PORT}"
# The scheduled jobs' names on each OS are in scheduler.py

OLD_MCP_NAME  = "zoho-timesheet"  # chat MCP server registered by earlier installs

# ── Claude Code hook ───────────────────────────────────────────────────────────
CLAUDE_SETTINGS  = Path.home() / ".claude" / "settings.json"
OLD_HOOK_FILE    = Path.home() / ".claude" / "hooks" / "timelog.sh"   # bash hook of earlier installs
# The hook's own settings are in hook.py: it doesn't import this module, to start fast.

# ── Counting time ──────────────────────────────────────────────────────────────
GAP_MIN       = 30   # a gap longer than this between prompts starts a new work block
TAIL_MIN      = 15   # added to every block for work after its last prompt
ROUND_MIN     = 15   # each draft's total is rounded to this
DAY_MINUTES   = 24 * 60
BACKLOG_DAYS  = 7    # how far back an argument-less `draft` looks for days without drafts
MISSED_WINDOW = 14   # how far back missed weekdays are reported
MAX_PROMPTS   = 40   # per repo, sent to Claude for the description
MAX_COMMITS   = 30   # per repo, sent to Claude for the description

OPEN  = ("pending", "failed")   # draft statuses that can still be accepted
MARKS = ("leave", "holiday")    # day marks that stop a day counting as missed

# ── Headless Claude ────────────────────────────────────────────────────────────
CLAUDE_MODEL     = os.getenv("TIMELOG_MODEL", "sonnet")            # (env) jobs and descriptions
MEETINGS_MODEL   = os.getenv("TIMELOG_MEETINGS_MODEL", "haiku")    # (env) reading the calendar
MEETINGS_TOOL    = "mcp__claude_ai_Microsoft_365__outlook_calendar_search"
# The connector's other tools, left out of the meetings call so their definitions cost no tokens
MEETINGS_OTHER_TOOLS = tuple("mcp__claude_ai_Microsoft_365__" + t for t in (
    "chat_message_search", "find_meeting_availability", "get_granted_scopes", "get_me", "outlook_email_search",
    "outlook_find_available_time", "read_resource", "search_people", "sharepoint_create_folder",
    "sharepoint_folder_search", "sharepoint_search", "sharepoint_update_file", "sharepoint_upload_file",
    "teams_list_channel_messages", "teams_list_channels", "teams_list_chats", "teams_list_teams"))
CLAUDE_TIMEOUT   = 600    # seconds per `claude -p` call
MEETINGS_TIMEOUT = 180
MCP_LIST_TIMEOUT = 60     # `claude mcp list` health-checks every server
GEN_LOCK_STALE   = 3600   # a generation lock older than this is from a crashed run

# ── Zoho ───────────────────────────────────────────────────────────────────────
ZOHO_DOMAIN  = os.getenv("ZOHO_DOMAIN", "zoho.in")   # (env)
ZOHO_API     = f"https://people.{ZOHO_DOMAIN}/people/api/timetracker"
ZOHO_ACCOUNTS = f"https://accounts.{ZOHO_DOMAIN}"
ZOHO_CONSOLE  = f"https://api-console.{ZOHO_DOMAIN}"   # where you create your Self Client
ZOHO_TOKEN_FILE = TIMELOG_DIR / "zoho_token.json"      # what Connect Zoho saves
ZOHO_SCOPES  = "ZOHOPEOPLE.timetracker.ALL,ZOHOPEOPLE.leave.READ,AaaServer.profile.Read"
ZOHO_TIMEOUT = 30        # seconds per request
TOKEN_TTL    = 45 * 60   # Zoho access tokens last an hour
CATALOG_TTL  = 10 * 60   # projects and jobs are re-read after this

# ── Message lengths ────────────────────────────────────────────────────────────
ERROR_CHARS = 300   # error text kept in drafts, API replies and logs
NOTE_CHARS  = 150   # error text inside a sentence (draft notes, notifications)
