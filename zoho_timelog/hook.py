"""Claude Code UserPromptSubmit hook (`timelog hook`): one activity line per prompt in
~/.timelog/YYYY-MM-DD.jsonl, which drafts.py turns into Zoho time logs.

It must never block or fail a session: no output (UserPromptSubmit stdout is added to Claude's
context), errors swallowed, always exit 0. It runs before every prompt, so it imports only the
standard library and oscompat (see __main__.py): not constants, whose config loading costs ~40 ms.
"""

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .oscompat import run

# Same as constants.TIMELOG_DIR, which also reads it from the process environment only
TIMELOG_DIR = Path(os.getenv("TIMELOG_DIR", "~/.timelog")).expanduser()
PROMPT_CHARS     = 200   # of each prompt kept in the day's jsonl
HOOK_GIT_TIMEOUT = 5     # seconds per git call

IDE_BLOCK = re.compile(r"<ide_[a-z_]+>.*?(</ide_[a-z_]+>|$)", re.S)  # $: cut off by the 200-char limit
INJECTED  = ("<task-notification", "<system-reminder", "<local-command")


def clean_prompt(prompt: str) -> str | None:
    """What you typed, or None for messages Claude Code injected itself (background task results,
    reminders), which aren't you working. IDE context blocks (<ide_opened_file>…) are dropped."""
    text = IDE_BLOCK.sub("", prompt or "").strip()
    if not text or text.startswith(INJECTED):
        return None
    return text


def main() -> int:
    if os.getenv("TIMELOG_DISABLE") == "1":   # our own `claude -p` runs
        return 0
    try:
        record(json.loads(sys.stdin.buffer.read().decode("utf-8", "replace")))
    except Exception:
        pass
    return 0


def record(data: dict, base: Path = TIMELOG_DIR) -> dict | None:
    """Append the line for one prompt; None (nothing written) for messages Claude Code injected itself."""
    prompt = clean_prompt(data.get("prompt") or "")
    if prompt is None:
        return None
    cwd = data.get("cwd") or os.getcwd()
    repo_path = _git(cwd, "rev-parse", "--show-toplevel") or cwd
    now = datetime.datetime.now().astimezone()
    line = {"ts": now.strftime("%Y-%m-%dT%H:%M:%S%z"), "repo": Path(repo_path).name, "repo_path": repo_path,
            "remote_group": remote_group(_git(repo_path, "remote", "get-url", "origin")),
            "branch": _git(repo_path, "branch", "--show-current"), "cwd": cwd,
            "session_id": data.get("session_id") or "", "prompt": prompt[:PROMPT_CHARS]}
    base.mkdir(parents=True, exist_ok=True)
    with open(base / f"{now.date().isoformat()}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line


def remote_group(remote: str) -> str:
    """GitLab/GitHub group of a remote URL, e.g. ssh://git@host:2224/codemonk/kawada/x.git → codemonk/kawada."""
    path = remote.strip().removesuffix(".git").removesuffix("/")
    if "://" in path:
        path = path.split("://", 1)[1]
        path = path.split("/", 1)[1] if "/" in path else path   # drop user@host:port
    else:
        path = path.split(":", 1)[1] if ":" in path else path   # scp form: git@host:group/repo
    return path.rsplit("/", 1)[0] if path and "/" in path else ""


def _git(cwd: str, *args: str) -> str:
    """git's output, or "" if it fails, isn't installed, or cwd isn't a repo."""
    try:
        proc = run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=HOOK_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""
