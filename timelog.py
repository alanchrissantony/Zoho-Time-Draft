"""Daily Zoho People time logs from your Claude Code activity and Outlook meetings — reviewed first.

hooks/timelog.sh records each Claude Code prompt in ~/.timelog/YYYY-MM-DD.jsonl. Each morning
`draft` turns finished days into draft time logs (one per Zoho project+job); you accept, edit or
decline them in the review app, and only accepted drafts are logged to Zoho.

Usage:
    python timelog.py calc    [DATE]               a day's timeline as JSON (local only, no meetings)
    python timelog.py draft   [DATE]               make drafts; no DATE = finished days without drafts,
                                                   then notify about missed days (what launchd runs)
    python timelog.py accept  DATE (--all | ID…)   log drafts to Zoho without the app
    python timelog.py app     [--port N]           run the review app (http://127.0.0.1:8765)
    python timelog.py install                      hook + launchd jobs
    python timelog.py status                       today's capture, drafts, missed days
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import drafts as dr
from constants import (APP_LABEL, APP_LOG, APP_URL, DRAFTS_DIR, HOST, LAUNCHD_LABEL, LAUNCHD_LOG, NOTE_CHARS,
                       OLD_MCP_NAME, PORT, PROJECTS_FILE, REPO_DIR)
from sources import load_activity, load_mapping


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


def cmd_app(args) -> None:
    import uvicorn
    from review_app import make_app
    uvicorn.run(make_app(args.port), host=HOST, port=args.port, log_level="warning")


# ── Install / status ───────────────────────────────────────────────────────────

def render_plist(name: str, values: dict) -> Path:
    plist = (REPO_DIR / "launchd" / f"{name}.plist").read_text()
    for key, val in values.items():
        plist = plist.replace(key, val)
    path = Path.home() / "Library" / "LaunchAgents" / f"{name}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist)
    return path


def load_agent(label: str, plist_path: Path) -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)


def agent_loaded(label: str) -> bool:
    return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True).returncode == 0


def cmd_install(args) -> None:
    home = Path.home()
    python = REPO_DIR / ".venv" / "bin" / "python"
    missing = [t for t in ("jq", "git", "claude") if not shutil.which(t)]
    if missing:
        sys.exit(f"Missing required tools: {', '.join(missing)}")
    if not python.exists():
        sys.exit("Create the venv first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    if not (REPO_DIR / ".env").exists():
        sys.exit("Copy .env.example to .env and fill in your Zoho credentials first.")

    # ~/.timelog, the mapping, and the day file (missed-day window starts 14 days back)
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    if not PROJECTS_FILE.exists():
        shutil.copy(REPO_DIR / "projects.example.json", PROJECTS_FILE)
        print(f"Seeded {PROJECTS_FILE}")
    dr.init_days_file(datetime.date.today())

    # Capture hook
    hook_link = home / ".claude" / "hooks" / "timelog.sh"
    hook_link.parent.mkdir(parents=True, exist_ok=True)
    if hook_link.is_symlink() or hook_link.exists():
        hook_link.unlink()
    hook_link.symlink_to(REPO_DIR / "hooks" / "timelog.sh")
    settings_path = home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    groups = settings.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    command = str(hook_link)
    if not any(h.get("command") == command for g in groups for h in g.get("hooks", [])):
        groups.append({"hooks": [{"type": "command", "command": command}]})
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        print(f"Registered UserPromptSubmit hook in {settings_path}")
    else:
        print("Hook already registered")

    # The chat MCP server is gone; unregister it so Claude Code doesn't start a deleted file
    if subprocess.run(["claude", "mcp", "get", OLD_MCP_NAME], capture_output=True).returncode == 0:
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", OLD_MCP_NAME], capture_output=True)
        print(f"Removed old MCP server '{OLD_MCP_NAME}'")

    # launchd: the draft job and the review app
    path_env = ":".join(dict.fromkeys([
        str(Path(shutil.which("claude")).parent), str(Path(shutil.which("jq")).parent),
        str(Path(shutil.which("git")).parent), "/usr/local/bin", "/opt/homebrew/bin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]))
    values = {"__PYTHON__": str(python), "__SCRIPT__": str(REPO_DIR / "timelog.py"), "__REPO__": str(REPO_DIR),
              "__PATH__": path_env, "__HOME__": str(home)}
    load_agent(LAUNCHD_LABEL, render_plist(LAUNCHD_LABEL, {
        **values, "__LABEL__": LAUNCHD_LABEL, "__LOG__": str(LAUNCHD_LOG)}))
    print(f"Loaded {LAUNCHD_LABEL} (makes drafts and checks missed days at login, 10:00 and 15:00)")
    load_agent(APP_LABEL, render_plist(APP_LABEL, {
        **values, "__LABEL__": APP_LABEL, "__PORT__": str(PORT), "__LOG__": str(APP_LOG)}))
    print(f"Loaded {APP_LABEL}: review app at {APP_URL}")
    if not shutil.which("terminal-notifier"):
        print("Tip: `brew install terminal-notifier` makes notifications open the review app when clicked.")


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
    for label in (LAUNCHD_LABEL, APP_LABEL):
        print(f"{label}: {'loaded' if agent_loaded(label) else 'NOT loaded (run: python timelog.py install)'}")
    print(f"Review app: {APP_URL}")
    try:
        rows = dr.overview(today)
    except Exception as exc:
        print(f"Zoho check failed: {exc}")
        return
    missed = [r["date"] for r in rows if r["state"] == "missed"]
    review = [f"{r['date']} ({r['open']})" for r in rows if r["open"]]
    print(f"Missed weekdays: {', '.join(missed) or 'none'}")
    print(f"Drafts to review: {', '.join(review) or 'none'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
    sub.add_parser("install", help="install hook and launchd jobs").set_defaults(func=cmd_install)
    sub.add_parser("status", help="show today's capture, drafts and missed days").set_defaults(func=cmd_status)
    args = parser.parse_args()
    if args.command == "accept" and not (args.all or args.ids):
        parser.error("accept needs --all or draft ids")
    args.func(args)


if __name__ == "__main__":
    main()
