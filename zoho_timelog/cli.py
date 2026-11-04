"""Daily Zoho People time logs from your Claude Code activity and Outlook meetings — reviewed first.

`timelog hook` records each Claude Code prompt in ~/.timelog/YYYY-MM-DD.jsonl. Each morning
`draft` turns finished days into draft time logs (one per Zoho project+job); you accept, edit or
decline them in the review app, and only accepted drafts are logged to Zoho.

Usage:
    timelog calc    [DATE]               a day's timeline as JSON (local only, no meetings)
    timelog draft   [DATE]               make drafts; no DATE = finished days without drafts,
                                         then notify about missed days (what the scheduled job runs)
    timelog accept  DATE (--all | ID…)   log drafts to Zoho without the app
    timelog app     [--port N]           run the review app (http://127.0.0.1:8765)
    timelog install                      hook + scheduled jobs (launchd, Task Scheduler or systemd)
    timelog uninstall                    remove them again (your data in ~/.timelog stays)
    timelog doctor                       check the setup and say what to fix
    timelog login                        connect Zoho with your own Self Client (also in the review app)
    timelog status                       today's capture, drafts, missed days
    timelog hook                         the Claude Code hook (reads the prompt event on stdin)
From a checkout, `python -m zoho_timelog …` is the same.
"""

import argparse
import datetime
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

from . import drafts as dr
from . import scheduler
from . import zoho_api as zoho
from .constants import (APP_LOG, APP_URL, CLAUDE_SETTINGS, CONFIG_FILE, DRAFTS_DIR, EXAMPLE_CONFIG,
                        EXAMPLE_PROJECTS, HOST, MCP_LIST_TIMEOUT, MISSED_WINDOW, NOTE_CHARS, OLD_HOOK_FILE,
                        OLD_MCP_NAME, PORT, PROJECTS_FILE, TIMELOG_DIR, ZOHO_CONSOLE, ZOHO_SCOPES)
from .oscompat import IS_MAC, IS_WINDOWS, run
from .sources import claude_cmd, load_activity, load_mapping


def parse_day(text: str | None) -> datetime.date:
    return datetime.date.fromisoformat(text) if text else datetime.date.today()


def cmd_calc(args) -> None:
    day = parse_day(args.date)
    timeline = dr.build_timeline(day, load_activity(day), [], load_mapping())
    for g in timeline["groups"]:
        g["hours"] = dr.hhmm(dr.round_minutes(g["raw"]))
    print(json.dumps(timeline, indent=2, default=str))


def cmd_draft(args) -> None:
    today = datetime.date.today()
    days = [parse_day(args.date)] if args.date else dr.days_to_generate(today)
    for day in days:
        try:
            data = dr.generate_day(day)
        except dr.Busy as exc:
            sys.exit(str(exc))
        except Exception as exc:  # one bad day (Zoho down, auth) shouldn't stop the others
            dr._append_log(f"\n## {day}  drafts FAILED {datetime.datetime.now():%Y-%m-%d %H:%M}\n- {exc}")
            continue
        print(f"{day}: {len(data['drafts'])} draft(s), {len(data['not_counted'])} not counted")
    if not days:
        print("No days to draft.")
    if args.date:
        return

    # Missed weekdays and open drafts (today excluded) → one notification
    try:
        rows = [r for r in reversed(dr.overview(today)) if r["state"] != "today"]
    except Exception as exc:
        dr.notify(f"Couldn't check Zoho for missed days: {str(exc)[:NOTE_CHARS]}")
        return
    missed = [datetime.date.fromisoformat(r["date"]) for r in rows if r["state"] == "missed"]
    review_days = [datetime.date.fromisoformat(r["date"]) for r in rows if r["open"]]
    message = dr.notification_text(missed, sum(r["open"] for r in rows))
    print(message or "Nothing missed, nothing to review.")
    if message:
        dr.notify(message, (missed or review_days)[0])


def cmd_accept(args) -> None:
    day = parse_day(args.date)
    if args.all:
        results = dr.accept_all(day)
    else:
        results = []
        for draft_id in args.ids:
            try:
                results.append(dr.submit(day, draft_id))
            except (ValueError, KeyError) as exc:
                print(f"{draft_id}: {exc}")
    if not results:
        print("Nothing accepted.")


def cmd_hook(args) -> None:
    """`timelog hook` is normally run by __main__ before this module loads; this is for direct calls."""
    from .hook import main as hook_main
    sys.exit(hook_main())


def cmd_app(args) -> None:
    import uvicorn
    from .review_app import make_app
    uvicorn.run(make_app(args.port), host=HOST, port=args.port, log_level="warning")


# ── Install / status ───────────────────────────────────────────────────────────

def self_command() -> list[str]:
    """How to start this tool again, for the hook and the scheduled jobs.

    The installed `timelog` launcher when that's what is running, else this Python with
    `-m zoho_timelog` (which needs the package installed in it, e.g. `uv tool install -e .`).
    """
    if Path(sys.argv[0]).suffix != ".py":
        exe = shutil.which(sys.argv[0])
        if exe:
            return [str(Path(exe).absolute())]
    return [sys.executable, "-m", "zoho_timelog"]


def hook_command() -> str:
    """The hook's command line. Quoted, with forward slashes, so it works however Claude Code runs it:
    a POSIX shell, or Git Bash or cmd on Windows."""
    def quote(part: str) -> str:
        return '"' + (part.replace("\\", "/") if IS_WINDOWS else part) + '"'
    return " ".join(quote(p) for p in self_command()) + " hook"


def _is_our_hook(command: str) -> bool:
    return command.endswith("hooks/timelog.sh") or (command.endswith(" hook") and "timelog" in command)


def update_hook_settings(settings_path: Path, command: str | None) -> bool:
    """Remove our UserPromptSubmit hooks from Claude Code's settings, then add `command` once (None: just
    remove). Other hooks are left alone; the file is backed up first. True if the file changed."""
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    hooks = settings.get("hooks", {})
    before = hooks.get("UserPromptSubmit", [])
    groups = []
    for group in before:
        kept = [h for h in group.get("hooks", []) if not _is_our_hook(h.get("command", ""))]
        if kept:
            groups.append({**group, "hooks": kept})
    if command:
        groups.append({"hooks": [{"type": "command", "command": command}]})
    if groups == before:
        return False
    if groups:
        hooks["UserPromptSubmit"] = groups
    else:
        hooks.pop("UserPromptSubmit", None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        shutil.copy(settings_path, settings_path.with_name(settings_path.name + ".bak"))
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True


def forward_old_hook(old_hook: Path, command: str) -> bool:
    """Claude Code reads its hooks when a session starts, so sessions open since an earlier install
    still run the old bash hook. Point it at the new one instead of leaving it to call a deleted
    script. True if it was rewritten."""
    forwarder = ("#!/bin/sh\n# Kept for Claude Code sessions started before `timelog hook` replaced this script.\n"
                 f"exec {command}\n")
    if not (old_hook.is_symlink() or old_hook.exists()):
        return False
    if not old_hook.is_symlink() and old_hook.read_text(encoding="utf-8") == forwarder:
        return False
    old_hook.unlink()
    old_hook.write_text(forwarder, encoding="utf-8")
    old_hook.chmod(0o755)
    return True


def cmd_install(args) -> None:
    missing = [t for t in ("git", "claude") if not shutil.which(t)]
    if missing:
        sys.exit(f"Missing required tools: {', '.join(missing)}")

    # ~/.timelog, the mapping, your settings, and the day file (missed-day window starts 14 days back)
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    for example, path in ((EXAMPLE_PROJECTS, PROJECTS_FILE), (EXAMPLE_CONFIG, CONFIG_FILE)):
        if not path.exists():
            shutil.copy(example, path)
            print(f"Seeded {path}")
    dr.init_days_file(datetime.date.today())

    # Capture hook
    command = hook_command()
    if update_hook_settings(CLAUDE_SETTINGS, command):
        print(f"Registered UserPromptSubmit hook in {CLAUDE_SETTINGS}: {command}")
    else:
        print("Hook already registered")
    if forward_old_hook(OLD_HOOK_FILE, command):
        print(f"{OLD_HOOK_FILE} now forwards to it (for Claude Code sessions started before this install)")

    # The chat MCP server is gone; unregister it so Claude Code doesn't start a deleted file
    if run([*claude_cmd(), "mcp", "get", OLD_MCP_NAME], capture_output=True).returncode == 0:
        run([*claude_cmd(), "mcp", "remove", "--scope", "user", OLD_MCP_NAME], capture_output=True)
        print(f"Removed old MCP server '{OLD_MCP_NAME}'")

    # Background jobs: the draft job and the review app
    failed = False
    try:
        for line in scheduler.install(self_command()):
            print(f"Installed {line}")
    except scheduler.Unavailable as exc:
        print(exc)
        failed = True
    except RuntimeError as exc:
        print(f"Couldn't schedule the jobs: {exc}")
        failed = True

    if not zoho.auth_status()["connected"]:
        print(f"Next: connect Zoho in the review app, {APP_URL} (or run: timelog login)")
        if not failed:
            webbrowser.open(APP_URL)
    if IS_MAC and not shutil.which("terminal-notifier"):
        print("Tip: `brew install terminal-notifier` makes notifications open the review app when clicked.")
    print("Check the setup with: timelog doctor")
    if failed:
        sys.exit(1)


def cmd_login(args) -> None:
    """Connect Zoho from the terminal: the same as the review app's Connect Zoho form."""
    import getpass
    if zoho.auth_status().get("source") == "env":
        print("Note: ZOHO_* values in your environment, config.env or .env take precedence over this connection.")
    print(f"1. Open {ZOHO_CONSOLE}, choose Self Client (create one if asked), and on its Client Secret tab copy\n"
          f"   the Client ID and Client Secret.\n"
          f"2. On the Generate Code tab, paste this scope, choose 10 minutes, type any description and press Create:\n"
          f"   {ZOHO_SCOPES}\n")
    client_id = input("Client ID: ")
    client_secret = getpass.getpass("Client Secret (not shown): ")
    code = input("Code: ")
    try:
        email = zoho.connect(client_id, client_secret, code)
    except ValueError as exc:
        if "email" not in str(exc):
            sys.exit(str(exc))
        email = zoho.connect(client_id, client_secret, input("New code: "), input("Your Zoho email: "))
    print(f"Connected as {email}.")


def cmd_uninstall(args) -> None:
    """Remove the jobs and the hook. Your data in ~/.timelog stays."""
    for line in scheduler.uninstall():
        print(line)
    if update_hook_settings(CLAUDE_SETTINGS, None):
        print(f"Removed the hook from {CLAUDE_SETTINGS}")
    old = OLD_HOOK_FILE
    if old.is_symlink() or (old.exists() and "timelog" in old.read_text(encoding="utf-8", errors="replace")):
        old.unlink()
        print(f"Removed {old}")
    print(f"Kept your data in {TIMELOG_DIR}. To remove the program as well: uv tool uninstall zoho-timelog")


def cmd_doctor(args) -> None:
    """One line per check: OK, or FIX with what to do. Exit code 1 if anything needs fixing."""
    problems = 0

    def check(ok: bool, good: str, fix: str) -> None:
        nonlocal problems
        print(f"OK   {good}" if ok else f"FIX  {fix}")
        problems += not ok

    check(sys.version_info >= (3, 11), f"Python {platform.python_version()}", "Python 3.11 or newer is needed")
    check(bool(shutil.which("git")), f"git: {shutil.which('git')}", "install git and put it on PATH")
    try:
        claude = " ".join(claude_cmd())
    except FileNotFoundError:
        claude = None
    check(bool(claude), f"Claude Code: {claude}", "install Claude Code, log in, and put `claude` on PATH")
    check(CONFIG_FILE.exists(), f"settings: {CONFIG_FILE}", "run: timelog install")

    settings = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8")) if CLAUDE_SETTINGS.exists() else {}
    registered = [h.get("command") for g in settings.get("hooks", {}).get("UserPromptSubmit", [])
                  for h in g.get("hooks", [])]
    command = hook_command()
    check(command in registered, f"Claude Code hook: {command}",
          "run: timelog install (the prompt hook isn't registered, or runs another copy of this tool)")
    if OLD_HOOK_FILE.exists():
        print(f"     {OLD_HOOK_FILE} forwards Claude Code sessions started before the Python hook; "
              "delete it once those sessions are restarted")

    for name, ok in scheduler.loaded().items():
        check(ok, f"scheduled: {name}", f"run: timelog install ({name} isn't loaded)")
    try:
        with urllib.request.urlopen(APP_URL, timeout=5) as resp:
            app_ok = resp.status == 200
    except OSError:
        app_ok = False
    check(app_ok, f"review app answers at {APP_URL}", f"the review app isn't answering at {APP_URL}: see {APP_LOG}")

    try:
        zoho.token()
        zoho_error = None
    except RuntimeError as exc:   # NotConnected included
        zoho_error = str(exc)
    status = zoho.auth_status()
    source = "ZOHO_* values in env/config.env/.env" if status.get("source") == "env" else "Connect Zoho"
    check(zoho_error is None, f"Zoho: signed in as {status.get('user') or '?'} ({source})", f"Zoho: {zoho_error}")
    if zoho_error is None:
        today = datetime.date.today()
        off, note = zoho.days_off(today - datetime.timedelta(days=MISSED_WINDOW), today, zoho.token())
        check(note is None, f"Zoho leave and holidays readable ({len(off)} day(s) off in the last {MISSED_WINDOW} days)",
              note or "")

    if os.getenv("TIMELOG_MEETINGS", "1") == "0":
        print("     Outlook meetings are off (TIMELOG_MEETINGS=0)")
    elif claude:
        print("     checking Claude connectors (claude mcp list can take a while)…", flush=True)
        try:
            listing = run([*claude_cmd(), "mcp", "list"], capture_output=True, text=True,
                          timeout=MCP_LIST_TIMEOUT, cwd=TIMELOG_DIR).stdout
        except (OSError, subprocess.SubprocessError):
            listing = ""
        connected = any(line.startswith("claude.ai Microsoft 365:") and line.rstrip().endswith("Connected")
                        for line in listing.splitlines())
        check(connected, "Microsoft 365 connector connected (Outlook meetings)",
              "connect Microsoft 365 in claude.ai → Settings → Connectors, or set TIMELOG_MEETINGS=0 in "
              f"{CONFIG_FILE}")
    print("All good." if not problems else f"{problems} thing{'s' if problems > 1 else ''} to fix.")
    sys.exit(1 if problems else 0)


def cmd_status(args) -> None:
    today = datetime.date.today()
    records = load_activity(today)
    print(f"Today ({today}): {len(records)} prompts captured")
    by_repo: dict[str, list] = {}
    for r in records:
        by_repo.setdefault(r["repo"], []).append(r["dt"])
    mapping = load_mapping()
    for repo, times in sorted(by_repo.items()):
        group = next((r.get("remote_group", "") for r in records if r["repo"] == repo), "")
        state, target = dr.resolve_mapping(repo, group, mapping)
        where = f"{target['project']} / {target.get('job', 'auto')}" if target else state.upper()
        print(f"  {repo:40} {dr.hhmm(dr.round_minutes(dr.block_minutes(times)))}  → {where}")
    for name, ok in scheduler.loaded().items():
        print(f"{name}: {'loaded' if ok else 'NOT loaded (run: timelog install)'}")
    print(f"Review app: {APP_URL}")
    try:
        rows = dr.overview(today)
    except Exception as exc:
        print(f"Zoho check failed: {exc}")
        return
    missed = [r["date"] for r in rows if r["state"] == "missed"]
    review = [f"{r['date']} ({r['open']})" for r in rows if r["open"]]
    off = [f"{r['date']} ({r['state']}{', from Zoho' if r['off'] == 'zoho' else ''})" for r in rows if r["off"]]
    print(f"Leave and holidays: {', '.join(off) or 'none'}")
    print(f"Missed weekdays: {', '.join(missed) or 'none'}")
    print(f"Drafts to review: {', '.join(review) or 'none'}")


def setup_output(log_path: str | None) -> None:
    """UTF-8 output everywhere (a Windows console or redirect would use cp1252 and fail on → · —).

    With --log, stdout and stderr append to that file instead: scheduled tasks on Windows capture
    no output, and under timelogw (pythonw) both streams are None, which uvicorn's logging can't use.
    """
    if log_path:
        path = Path(log_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        elif hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", metavar="PATH", help="append all output to this file (used by the scheduled jobs)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("calc", help="print a day's timeline (local only)")
    p.add_argument("date", nargs="?", help="YYYY-MM-DD (default: today)")
    p.set_defaults(func=cmd_calc)
    p = sub.add_parser("draft", help="make drafts and check for missed days")
    p.add_argument("date", nargs="?", help="YYYY-MM-DD to (re)generate one day; default: finished days without drafts")
    p.set_defaults(func=cmd_draft)
    p = sub.add_parser("accept", help="log drafts to Zoho")
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("ids", nargs="*", help="draft ids (see ~/.timelog/drafts/DATE.json)")
    p.add_argument("--all", action="store_true", help="every open draft of the day")
    p.set_defaults(func=cmd_accept)
    p = sub.add_parser("app", help="run the review app")
    p.add_argument("--port", type=int, default=PORT)
    p.set_defaults(func=cmd_app)
    sub.add_parser("install", help="install the hook and the scheduled jobs").set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="remove the hook and the scheduled jobs").set_defaults(func=cmd_uninstall)
    sub.add_parser("doctor", help="check the setup and say what to fix").set_defaults(func=cmd_doctor)
    sub.add_parser("login", help="connect Zoho from the terminal (same as the review app)").set_defaults(func=cmd_login)
    sub.add_parser("status", help="show today's capture, drafts and missed days").set_defaults(func=cmd_status)
    sub.add_parser("hook", help="Claude Code prompt hook (reads the event on stdin)").set_defaults(func=cmd_hook)
    args = parser.parse_args()
    setup_output(args.log)
    if args.command == "accept" and not (args.all or args.ids):
        parser.error("accept needs --all or draft ids")
    args.func(args)


if __name__ == "__main__":
    main()
