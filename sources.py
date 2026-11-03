"""Activity sources for one day. Nothing here decides projects; drafts.py does.

    claude    ~/.timelog/DATE.jsonl written by hooks/timelog.sh     points (prompts)
    meetings  Outlook calendar via the claude.ai Microsoft 365       spans
              connector (headless `claude -p`, calendar read tool only)
    git       your commits in a repo, used for descriptions
"""

import datetime
import json
import os
import re
import subprocess
from pathlib import Path

from constants import (CLAUDE_MODEL, CLAUDE_TIMEOUT, ERROR_CHARS, MAX_COMMITS, MCP_LIST_TIMEOUT,
                       MEETINGS_MODEL, MEETINGS_OTHER_TOOLS, MEETINGS_TIMEOUT, MEETINGS_TOOL, NOTE_CHARS,
                       PROJECTS_FILE, TIMELOG_DIR)

# What a failed headless `claude -p` call can raise (see run_claude)
CLAUDE_ERRORS = (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError)


def load_mapping() -> dict:
    return json.loads(PROJECTS_FILE.read_text()) if PROJECTS_FILE.exists() else {}


def day_bounds(day: datetime.date) -> tuple[datetime.datetime, datetime.datetime]:
    """Local midnight to midnight, timezone-aware."""
    start = datetime.datetime.combine(day, datetime.time.min).astimezone()
    return start, start + datetime.timedelta(days=1)


# ── Claude Code prompts ────────────────────────────────────────────────────────

IDE_BLOCK = re.compile(r"<ide_[a-z_]+>.*?(</ide_[a-z_]+>|$)", re.S)  # $: cut off by the 200-char limit
INJECTED  = ("<task-notification", "<system-reminder", "<local-command")


def clean_prompt(prompt: str) -> str | None:
    """What you typed, or None for messages Claude Code injected itself (same rules as the hook,
    applied again here for lines captured before the hook filtered them)."""
    text = IDE_BLOCK.sub("", prompt or "").strip()
    if not text or text.startswith(INJECTED):
        return None
    return text


def load_activity(day: datetime.date, base: Path = TIMELOG_DIR) -> list[dict]:
    """Read a day's hook jsonl, skipping malformed lines and injected messages.
    Adds a parsed 'dt' to each record."""
    path = base / f"{day.isoformat()}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
            rec["dt"] = datetime.datetime.fromisoformat(rec["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        prompt = clean_prompt(rec.get("prompt", ""))
        if rec.get("repo") and prompt is not None:
            records.append({**rec, "prompt": prompt})
    return records


# ── Git ────────────────────────────────────────────────────────────────────────

def git_commits(path: str, day: datetime.date) -> list[dict]:
    """Your commits (any branch) in one repo on a day: [{"dt", "subject"}], oldest first."""
    if not path or not Path(path, ".git").exists():
        return []
    author = subprocess.run(["git", "-C", path, "config", "user.email"],
                            capture_output=True, text=True).stdout.strip()
    cmd = ["git", "-C", path, "log", "--all", "--no-merges", "--format=%aI%x09%s",
           f"--since={day.isoformat()} 00:00", f"--until={day.isoformat()} 23:59:59"]
    if author:
        cmd += [f"--author={author}", "--regexp-ignore-case"]
    commits = []
    for line in subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[:MAX_COMMITS]:
        ts, _, subject = line.partition("\t")
        try:
            commits.append({"dt": datetime.datetime.fromisoformat(ts), "subject": subject})
        except ValueError:
            continue
    return sorted(commits, key=lambda c: c["dt"])


# ── Headless Claude ────────────────────────────────────────────────────────────

def run_claude(prompt: str, schema: dict, model: str = CLAUDE_MODEL, allowed_tools: str = "",
               timeout: int = CLAUDE_TIMEOUT, meta: bool = False, disallowed_tools=()):
    """One headless `claude -p` call returning schema'd JSON (and the raw result when meta=True).

    No built-in tools. With allowed_tools empty there are no MCP servers either; otherwise
    only those MCP tools are permitted (anything else is denied in -p mode). disallowed_tools
    (tool names or server prefixes) are left out of the context entirely, which saves tokens.
    """
    cmd = ["claude", "-p", "--model", model, "--output-format", "json",
           "--json-schema", json.dumps(schema), "--tools", "", "--no-session-persistence"]
    cmd += ["--allowedTools", allowed_tools] if allowed_tools else ["--strict-mcp-config"]
    if disallowed_tools:
        cmd += ["--disallowedTools", *disallowed_tools]  # the prompt goes on stdin, so nothing follows it
    env = {**os.environ, "TIMELOG_DISABLE": "1"}  # don't record our own runs as activity
    TIMELOG_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          timeout=timeout, env=env, cwd=TIMELOG_DIR)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:ERROR_CHARS]}")
    out = json.loads(proc.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {str(out.get('result'))[:ERROR_CHARS]}")
    data = out.get("structured_output")
    if data is None:
        text = (out.get("result") or "").strip().removeprefix("```json").removesuffix("```")
        data = json.loads(text)
    return (data, out) if meta else data


_servers_cache: list[str] | None = None


def other_mcp_servers() -> list[str]:
    """Tool prefixes of every connected MCP server except the calendar's, e.g. "mcp__claude_ai_Figma".

    Read from `claude mcp list` (costs no tokens), once per process. [] if it can't be read:
    the meetings call then just carries their tool definitions, as it would without this.
    """
    global _servers_cache
    if _servers_cache is None:
        try:
            listing = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True,
                                     timeout=MCP_LIST_TIMEOUT, cwd=TIMELOG_DIR).stdout
        except (OSError, subprocess.SubprocessError):
            listing = ""
        _servers_cache = parse_mcp_list(listing)
    return _servers_cache


def parse_mcp_list(listing: str) -> list[str]:
    own = MEETINGS_TOOL.rsplit("__", 1)[0]
    prefixes = []
    for line in listing.splitlines():
        name, sep, rest = line.partition(": ")
        if not sep or not rest.rstrip().endswith("Connected"):
            continue
        name = "claude_ai_" + name.removeprefix("claude.ai ") if name.startswith("claude.ai ") else name
        prefix = "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", name)
        if prefix != own:
            prefixes.append(prefix)
    return prefixes


# ── Outlook meetings ───────────────────────────────────────────────────────────

MEETINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "meetings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
                "show_as": {"type": "string"}, "all_day": {"type": "boolean"}, "cancelled": {"type": "boolean"},
            },
            "required": ["subject", "start", "end", "show_as", "all_day", "cancelled"],
        }},
    },
    "required": ["meetings"],
}

MEETINGS_PROMPT = """List every event on my Outlook calendar on {date}.
Call outlook_calendar_search with query "*", afterDateTime "{date}T00:00:00",
beforeDateTime "{next}T00:00:00", order "oldest", limit 25, offset 0. If the result ends with a
nextOffset, call it again with that offset until there are no more pages.
Return each event exactly as given: subject copied character for character, start and end as
ISO 8601 with the UTC offset of the event's timeZone, show_as from showAs, all_day from isAllDay,
cancelled from isCancelled. Return an empty list if there are none. Do nothing else."""


def parse_meetings(raw: list[dict], day: datetime.date) -> list[dict]:
    """Keep meetings that took your time: not all-day, not cancelled, not 'free'.
    Times become local and are clipped to the day."""
    lo, hi = day_bounds(day)
    tz = lo.tzinfo
    meetings = []
    for m in raw:
        if m.get("all_day") or m.get("cancelled") or str(m.get("show_as", "")).lower() == "free":
            continue
        try:
            start, end = (datetime.datetime.fromisoformat(m[k]) for k in ("start", "end"))
        except (KeyError, TypeError, ValueError):
            continue
        start, end = (t.astimezone() if t.tzinfo else t.replace(tzinfo=tz) for t in (start, end))
        start, end = max(start, lo), min(end, hi)
        subject = (m.get("subject") or "").strip()
        if end > start and subject:
            meetings.append({"subject": subject, "start": start, "end": end})
    return sorted(meetings, key=lambda m: m["start"])


def outlook_meetings(day: datetime.date) -> tuple[list[dict], str | None]:
    """The day's meetings [{"subject", "start", "end"}], plus a note if they couldn't be read.

    Only subject, times and status are asked for; bodies and join links are never kept.
    TIMELOG_MEETINGS=0 turns this off.
    """
    if os.getenv("TIMELOG_MEETINGS", "1") == "0":
        return [], None
    prompt = MEETINGS_PROMPT.format(date=day.isoformat(), next=(day + datetime.timedelta(days=1)).isoformat())
    hint = "Check that the Microsoft 365 connector is connected in claude.ai, then regenerate."
    # Only the calendar tool is loaded: other connectors' definitions were ~85% of this call's tokens
    leave_out = [*other_mcp_servers(), *MEETINGS_OTHER_TOOLS]
    for _ in range(2):
        try:
            data, out = run_claude(prompt, MEETINGS_SCHEMA, model=MEETINGS_MODEL, allowed_tools=MEETINGS_TOOL,
                                   timeout=MEETINGS_TIMEOUT, meta=True, disallowed_tools=leave_out)
        except json.JSONDecodeError:
            continue  # replied in prose without searching (Haiku sometimes does); ask again
        except CLAUDE_ERRORS as exc:
            return [], f"Outlook meetings not read ({str(exc)[:NOTE_CHARS]}). {hint}"
        if out.get("permission_denials"):
            return [], f"Outlook meetings not read (calendar tool was denied). {hint}"
        # An empty answer is only trusted if the calendar was actually searched (a tool turn).
        if data.get("meetings") or (out.get("num_turns") or 0) >= 2:
            return parse_meetings(data.get("meetings", []), day), None
    return [], f"Outlook meetings not read (the calendar wasn't searched). {hint}"
