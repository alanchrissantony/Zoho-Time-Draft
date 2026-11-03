"""Install → hook → uninstall on a throwaway machine, as a teammate would (CI runs it on Linux,
Windows and macOS). Run it after `uv tool install .` has put `timelog` on PATH.

It changes ~/.claude/settings.json and the scheduled jobs of the user running it, so it refuses to
run unless CI=true or --yes-this-is-a-throwaway-machine is given. Standard library only.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

WINDOWS, MAC = sys.platform == "win32", sys.platform == "darwin"
HOME = Path.home()
TIMELOG_DIR = Path(os.getenv("TIMELOG_DIR", HOME / ".timelog"))
SETTINGS = HOME / ".claude" / "settings.json"
APP = "http://127.0.0.1:8765/api/auth"
PROMPT = "ci → ✓ café"
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")

failures: list[str] = []
GITHUB = os.getenv("GITHUB_ACTIONS") == "true"


def annotate(level: str, title: str, text: str) -> None:
    """On GitHub, also show it on the run page (readable without logging in, unlike the log)."""
    if GITHUB:
        body = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level} title=smoke {sys.platform}: {title}::{body}", flush=True)


def check(ok: bool, what: str) -> bool:
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)
        annotate("error", "FAIL", what)
    return ok


def note(text: str) -> None:
    print("NOTE " + text, flush=True)


def run(cmd, **kw) -> subprocess.CompletedProcess:
    print("$ " + (cmd if isinstance(cmd, str) else " ".join(map(str, cmd))), flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if (proc.stdout + proc.stderr).strip():
        print((proc.stdout + proc.stderr).strip(), flush=True)
    return proc


def app_answers(seconds: int = 20) -> bool:
    for _ in range(seconds):
        try:
            with urllib.request.urlopen(APP, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            time.sleep(1)
    return False


def our_hook_commands() -> list[str]:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    return [h.get("command", "") for g in settings.get("hooks", {}).get("UserPromptSubmit", [])
            for h in g.get("hooks", []) if "timelog" in h.get("command", "")]


def jobs_present() -> dict[str, bool]:
    if WINDOWS:
        return {t: run(["schtasks", "/Query", "/TN", t]).returncode == 0
                for t in ("codemonk-timelog-draft", "codemonk-timelog-app")}
    if MAC:
        uid = os.getuid()
        return {label: run(["launchctl", "print", f"gui/{uid}/{label}"]).returncode == 0
                for label in ("com.codemonk.timelog", "com.codemonk.timelog.app")}
    units = HOME / ".config" / "systemd" / "user"
    return {name: (units / name).exists() for name in ("timelog-draft.timer", "timelog-app.service")}


def hook_writes_a_line(how: str, cmd: list[str] | str, repo: Path) -> None:
    """Send one prompt event through the registered hook command, as Claude Code does."""
    today = TIMELOG_DIR / f"{datetime.date.today().isoformat()}.jsonl"
    before = today.read_text(encoding="utf-8").splitlines() if today.exists() else []
    event = json.dumps({"cwd": str(repo), "prompt": f"{PROMPT} ({how})", "session_id": "ci"})
    proc = subprocess.run(cmd, input=event.encode("utf-8"), capture_output=True, shell=isinstance(cmd, str))
    after = today.read_text(encoding="utf-8").splitlines() if today.exists() else []
    new = [json.loads(line) for line in after[len(before):]]
    check(proc.returncode == 0 and not proc.stdout, f"hook via {how}: exit 0, no output (got {proc.returncode})")
    if check(len(new) == 1, f"hook via {how}: one line written (got {len(new)})"):
        check(new[0]["prompt"] == f"{PROMPT} ({how})", f"hook via {how}: prompt intact: {new[0]['prompt']!r}")
        check(new[0]["repo"] == repo.name and new[0]["branch"] == "main",
              f"hook via {how}: repo and branch from git ({new[0]['repo']}, {new[0]['branch']!r})")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if os.getenv("CI") != "true" and "--yes-this-is-a-throwaway-machine" not in sys.argv:
        sys.exit(__doc__)

    # A stand-in `claude` (this machine has no Claude Code), and a git repo to work in
    stub = Path(tempfile.mkdtemp(prefix="claude-stub-"))
    if WINDOWS:
        (stub / "claude.cmd").write_text("@exit /b 1\r\n")
    else:
        (stub / "claude").write_text("#!/bin/sh\nexit 1\n")
        (stub / "claude").chmod(0o755)
    os.environ["PATH"] = str(stub) + os.pathsep + os.environ["PATH"]
    repo = Path(tempfile.mkdtemp(prefix="ci-repo-")) / "kawada-api"
    repo.mkdir()
    run(["git", "init", "-q", "-b", "main", str(repo)])

    check(bool(shutil.which("timelog")), f"timelog on PATH: {shutil.which('timelog')}")

    print("\n== install")
    if MAC:   # launchd agents live in the GUI session; note whether this machine has one
        gui = run(["launchctl", "print", f"gui/{os.getuid()}"])
        note(f"GUI session gui/{os.getuid()}: {'present' if gui.returncode == 0 else 'missing'}")
        annotate("notice", "GUI session", f"gui/{os.getuid()} {'present' if gui.returncode == 0 else 'missing'}")
    installed = run(["timelog", "install"])
    jobs = jobs_present()
    if WINDOWS:   # proves Task Scheduler accepted the generated XML for a non-admin task
        for name, ok in jobs.items():
            check(ok, f"scheduled task {name} exists")
    elif MAC:
        for name, ok in jobs.items():
            check(ok, f"launchd agent {name} loaded")
    elif all(jobs.values()):
        check(run(["systemctl", "--user", "is-active", "timelog-draft.timer", "timelog-app.service"]).returncode == 0,
              "systemd units active")
    else:
        check("crontab -e" in installed.stdout, "no systemd user session: install printed the crontab fallback")
    answers = app_answers()
    if MAC:
        check(answers, f"review app answers at {APP}")
    else:   # the CI user may have no session to run it in; a desktop always does
        note(f"review app {'answers' if answers else 'did not answer'} at {APP}")

    print("\n== hook")
    commands = our_hook_commands()
    if check(len(commands) == 1, f"one hook registered in {SETTINGS}: {commands}"):
        hook_writes_a_line("the shell" + (" (cmd.exe)" if WINDOWS else ""), commands[0], repo)
        if WINDOWS and GIT_BASH.exists():
            hook_writes_a_line("Git Bash", [str(GIT_BASH), "-c", commands[0]], repo)

    print("\n== doctor (FIX for Zoho and Claude is expected here)")
    run(["timelog", "doctor"])

    print("\n== uninstall")
    run(["timelog", "uninstall"])
    check(our_hook_commands() == [], "hook removed from settings.json")
    check(not any(jobs_present().values()), "scheduled jobs removed")

    print(f"\n{len(failures)} failure(s)" + "".join(f"\n  - {f}" for f in failures))
    if failures:
        annotate("error", f"timelog install exited {installed.returncode}",
                 (installed.stdout + installed.stderr)[-2500:])
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
