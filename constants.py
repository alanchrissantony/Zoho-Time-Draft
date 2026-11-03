"""Settings, paths and limits shared by every module. Importing this loads the repo's .env.

Values marked (env) can be overridden in .env or the environment.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_DIR = Path(__file__).resolve().parent
# Load .env from the repo, not the cwd — launchd starts us elsewhere.
load_dotenv(REPO_DIR / ".env")

# ── Paths ──────────────────────────────────────────────────────────────────────
TIMELOG_DIR   = Path(os.getenv("TIMELOG_DIR", "~/.timelog")).expanduser()   # (env)
PROJECTS_FILE = TIMELOG_DIR / "projects.json"
DRAFTS_DIR    = TIMELOG_DIR / "drafts"
DAYS_FILE     = TIMELOG_DIR / "days.json"
RUN_LOG       = TIMELOG_DIR / "run.log"
GEN_LOCK      = TIMELOG_DIR / ".lock"
LAUNCHD_LOG   = TIMELOG_DIR / "launchd.log"
APP_LOG       = TIMELOG_DIR / "app.log"
ZOHO_LOG_FILE = Path(os.getenv("LOG_FILE", TIMELOG_DIR / "zoho_submissions.log")).expanduser()  # (env)
PROMPT_FILE   = REPO_DIR / "prompts" / "nightly.md"
INDEX_HTML    = REPO_DIR / "web" / "index.html"

# ── Review app and launchd ─────────────────────────────────────────────────────
HOST    = "127.0.0.1"   # the app only ever listens here
PORT    = int(os.getenv("TIMELOG_PORT", "8765"))   # (env)
APP_URL = f"http://{HOST}:{PORT}"

LAUNCHD_LABEL = "com.codemonk.timelog"
APP_LABEL     = "com.codemonk.timelog.app"
OLD_MCP_NAME  = "zoho-timesheet"  # chat MCP server registered by earlier installs

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
ZOHO_TIMEOUT = 30        # seconds per request
TOKEN_TTL    = 45 * 60   # Zoho access tokens last an hour
CATALOG_TTL  = 10 * 60   # projects and jobs are re-read after this

# ── Message lengths ────────────────────────────────────────────────────────────
ERROR_CHARS = 300   # error text kept in drafts, API replies and logs
NOTE_CHARS  = 150   # error text inside a sentence (draft notes, notifications)
