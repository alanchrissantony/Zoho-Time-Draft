"""Background jobs on each OS: the draft job (at login, 10:00 and 15:00, catching up after sleep)
and the review app (started at login, restarted if it exits).

    macOS    launchd agents in ~/Library/LaunchAgents
    Windows  Task Scheduler tasks for the current user (no admin, no password)
    Linux    systemd user units; without systemd, the cron lines to add by hand

The render_* functions are pure, so the tests check every OS's files on any OS.
"""

import os
import plistlib
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from xml.sax.saxutils import escape

from . import oscompat
from .constants import APP_LOG, APP_URL, SCHED_LOG, TIMELOG_DIR
from .oscompat import run

JOBS        = ("draft", "app")
LAUNCHD     = {"draft": "com.codemonk.timelog", "app": "com.codemonk.timelog.app"}   # labels of earlier installs
TASKS       = {"draft": "codemonk-timelog-draft", "app": "codemonk-timelog-app"}     # root folder: no admin needed
UNITS       = {"draft": "timelog-draft.timer", "app": "timelog-app.service"}
LOGS        = {"draft": SCHED_LOG, "app": APP_LOG}
DRAFT_HOURS = (10, 15)


class Unavailable(RuntimeError):
    """No scheduler we can use on this machine; the message says what to set up by hand."""


def job_args(cmd: list[str], job: str) -> list[str]:
    return [*cmd, "--log", str(LOGS[job]), job]


def job_path(cmd: list[str]) -> str:
    """PATH for the jobs: schedulers don't load your shell profile, so claude, git and node (for npm's
    claude.cmd) are found where they are now, plus this tool's own folder and the OS defaults."""
    dirs = [str(Path(p).parent) for p in map(shutil.which, ("claude", "git", "node")) if p]
    if Path(cmd[0]).is_absolute():
        dirs.append(str(Path(cmd[0]).parent))
    if oscompat.IS_MAC:
        dirs += ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    elif not oscompat.IS_WINDOWS:
        dirs += ["/usr/local/bin", "/usr/bin", "/bin"]
    return os.pathsep.join(dict.fromkeys(dirs))


def install(cmd: list[str]) -> list[str]:
    """(Re)install both jobs to run `cmd`; returns what was done, one line each."""
    TIMELOG_DIR.mkdir(parents=True, exist_ok=True)
    if oscompat.IS_MAC:
        return _launchd_install(cmd)
    if oscompat.IS_WINDOWS:
        return _tasks_install(cmd)
    return _systemd_install(cmd)


def uninstall() -> list[str]:
    if oscompat.IS_MAC:
        return _launchd_uninstall()
    if oscompat.IS_WINDOWS:
        return _tasks_uninstall()
    return _systemd_uninstall()


def loaded() -> dict[str, bool]:
    """{job name as the OS shows it: installed and active}."""
    if oscompat.IS_MAC:
        return {LAUNCHD[j]: _ok(["launchctl", "print", f"{_domain()}/{LAUNCHD[j]}"]) for j in JOBS}
    if oscompat.IS_WINDOWS:
        return {TASKS[j]: _ok(["schtasks", "/Query", "/TN", TASKS[j]]) for j in JOBS}
    if not _ok(["systemctl", "--user", "show-environment"]):   # the cron fallback: are the lines there?
        try:
            crontab = run(["crontab", "-l"], capture_output=True, text=True).stdout
        except OSError:
            crontab = ""
        return {"crontab (no systemd here)": all(f"--log {LOGS[j]} {j}" in crontab for j in JOBS)}
    return {UNITS[j]: _ok(["systemctl", "--user", "is-active", "--quiet", UNITS[j]]) for j in JOBS}


def _ok(cmd: list[str]) -> bool:
    try:
        return run(cmd, capture_output=True).returncode == 0
    except OSError:
        return False


def _must(cmd: list[str]) -> None:
    try:
        proc = run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(f"{cmd[0]} failed: {exc}") from None
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {(proc.stderr or proc.stdout).strip()}")


# ── macOS: launchd ─────────────────────────────────────────────────────────────

def render_launchd(job: str, cmd: list[str], path_env: str) -> bytes:
    plist = {
        "Label": LAUNCHD[job],
        "ProgramArguments": job_args(cmd, job),
        "WorkingDirectory": str(TIMELOG_DIR),
        "EnvironmentVariables": {"PATH": path_env, "HOME": str(Path.home())},
        "RunAtLoad": True,
        # Anything printed before --log takes over (a crash at start-up) still reaches the log
        "StandardOutPath": str(LOGS[job]),
        "StandardErrorPath": str(LOGS[job]),
    }
    if job == "draft":   # a time slept through fires on wake
        plist["StartCalendarInterval"] = [{"Hour": h, "Minute": 0} for h in DRAFT_HOURS]
    else:
        plist.update(KeepAlive=True, ThrottleInterval=30)
    return plistlib.dumps(plist)


def _plist_path(job: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD[job]}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchd_install(cmd: list[str]) -> list[str]:
    path_env = job_path(cmd)
    for job in JOBS:
        path = _plist_path(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_launchd(job, cmd, path_env))
        run(["launchctl", "bootout", f"{_domain()}/{LAUNCHD[job]}"], capture_output=True)
        try:
            _must(["launchctl", "bootstrap", _domain(), str(path)])
        except RuntimeError:   # bootout finishes asynchronously; give it a moment
            time.sleep(1)
            _must(["launchctl", "bootstrap", _domain(), str(path)])
    return [f"launchd: {LAUNCHD['draft']} (drafts at login, 10:00 and 15:00)",
            f"launchd: {LAUNCHD['app']} (review app at {APP_URL})"]


def _launchd_uninstall() -> list[str]:
    done = []
    for job in JOBS:
        run(["launchctl", "bootout", f"{_domain()}/{LAUNCHD[job]}"], capture_output=True)
        if _plist_path(job).exists():
            _plist_path(job).unlink()
            done.append(f"removed launchd agent {LAUNCHD[job]}")
    return done


# ── Windows: Task Scheduler ────────────────────────────────────────────────────

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>{calendar}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>{time_limit}</ExecutionTimeLimit>{restart}
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

_CALENDAR = """
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{hour:02d}:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>"""

_RESTART = """
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>"""


def render_task_xml(job: str, cmd: list[str], user: str) -> str:
    """The task, as schtasks /XML wants it (write it UTF-16). The exe runs with no console window."""
    args = job_args(cmd, job)
    draft = job == "draft"
    return _TASK_XML.format(
        description=escape("Makes Zoho time-log drafts at logon, 10:00 and 15:00" if draft
                           else f"Zoho time-log review app at {APP_URL}"),
        user=escape(user),
        calendar="".join(_CALENDAR.format(hour=h) for h in DRAFT_HOURS) if draft else "",
        time_limit="PT2H" if draft else "PT0S",   # PT0S: the app runs until logoff
        restart="" if draft else _RESTART,
        command=escape(args[0]),
        arguments=escape(subprocess.list2cmdline(args[1:])),
        workdir=escape(str(TIMELOG_DIR)),
    )


def _windowless(cmd: list[str]) -> list[str]:
    """timelogw.exe (no console window) next to timelog.exe, when there is one."""
    exe = Path(cmd[0])
    quiet = exe.with_name("timelogw.exe")
    return [str(quiet), *cmd[1:]] if exe.name.lower() == "timelog.exe" and quiet.exists() else cmd


def _tasks_install(cmd: list[str]) -> list[str]:
    cmd = _windowless(cmd)
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    run(["schtasks", "/End", "/TN", TASKS["app"]], capture_output=True)   # the old app, if running
    for job in JOBS:
        xml = TIMELOG_DIR / f"{TASKS[job]}.xml"
        xml.write_text(render_task_xml(job, cmd, user), encoding="utf-16")
        _must(["schtasks", "/Create", "/TN", TASKS[job], "/XML", str(xml), "/F"])
    _must(["schtasks", "/Run", "/TN", TASKS["app"]])
    return [f"Task Scheduler: {TASKS['draft']} (drafts at logon, 10:00 and 15:00)",
            f"Task Scheduler: {TASKS['app']} (review app at {APP_URL})"]


def _tasks_uninstall() -> list[str]:
    run(["schtasks", "/End", "/TN", TASKS["app"]], capture_output=True)
    done = []
    for job in JOBS:
        if _ok(["schtasks", "/Delete", "/TN", TASKS[job], "/F"]):
            done.append(f"removed scheduled task {TASKS[job]}")
        (TIMELOG_DIR / f"{TASKS[job]}.xml").unlink(missing_ok=True)
    return done


# ── Linux: systemd user units ──────────────────────────────────────────────────

def _sd_quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_systemd(cmd: list[str], path_env: str) -> dict[str, str]:
    def service(job: str, description: str, extra: str) -> str:
        exec_start = " ".join(_sd_quote(a) for a in job_args(cmd, job))
        return (f"[Unit]\nDescription={description}\n\n[Service]\n{extra}"
                f"ExecStart={exec_start}\nEnvironment={_sd_quote('PATH=' + path_env)}\n")
    hours = ",".join(str(h) for h in DRAFT_HOURS)
    return {
        "timelog-app.service": service("app", f"Zoho time-log review app ({APP_URL})",
                                       "Restart=always\nRestartSec=30\n")
                               + "\n[Install]\nWantedBy=default.target\n",
        "timelog-draft.service": service("draft", "Make Zoho time-log drafts", "Type=oneshot\n"),
        "timelog-draft.timer": ("[Unit]\nDescription=Make Zoho time-log drafts at login, "
                                + " and ".join(f"{h}:00" for h in DRAFT_HOURS) + "\n\n"
                                f"[Timer]\nOnCalendar=*-*-* {hours}:00:00\n"
                                "OnStartupSec=1min\n"   # the user manager starts at login
                                "Persistent=true\n"     # a time missed while off runs at next start
                                "\n[Install]\nWantedBy=timers.target\n"),
    }


def _units_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def cron_lines(cmd: list[str]) -> str:
    def line(job: str) -> str:
        return " ".join(shlex.quote(a) for a in job_args(cmd, job))
    hours = ",".join(str(h) for h in DRAFT_HOURS)
    return f"PATH={job_path(cmd)}\n0 {hours} * * * {line('draft')}\n@reboot {line('app')}"


def _systemd_install(cmd: list[str]) -> list[str]:
    if not _ok(["systemctl", "--user", "show-environment"]):
        raise Unavailable("No systemd user session here (WSL without systemd, a container…), so nothing was "
                          "scheduled. Add these lines with `crontab -e`:\n\n" + cron_lines(cmd))
    _units_dir().mkdir(parents=True, exist_ok=True)
    for name, text in render_systemd(cmd, job_path(cmd)).items():
        (_units_dir() / name).write_text(text, encoding="utf-8")
    _must(["systemctl", "--user", "daemon-reload"])
    _must(["systemctl", "--user", "enable", "--now", UNITS["draft"], UNITS["app"]])
    _must(["systemctl", "--user", "restart", UNITS["app"]])   # pick up a new exe on reinstall
    return [f"systemd: {UNITS['draft']} (drafts at login, 10:00 and 15:00)",
            f"systemd: {UNITS['app']} (review app at {APP_URL})"]


def _systemd_uninstall() -> list[str]:
    # _ok, not run: on a machine without systemd (the cron fallback) systemctl doesn't exist
    _ok(["systemctl", "--user", "disable", "--now", UNITS["draft"], UNITS["app"]])
    done = []
    for name in render_systemd(["timelog"], ""):
        if (_units_dir() / name).exists():
            (_units_dir() / name).unlink()
            done.append(f"removed {name}")
    _ok(["systemctl", "--user", "daemon-reload"])
    return done
