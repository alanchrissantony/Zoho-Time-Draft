"""The differences between macOS, Windows and Linux, kept in one place.

    run        subprocess.run with UTF-8 text (Windows defaults to cp1252) and no console window
    file_lock  exclusive lock on a file: fcntl on macOS/Linux, msvcrt on Windows
    notify     a desktop notification that opens a URL when clicked, where the OS allows it
"""

import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC     = sys.platform == "darwin"

# Windows: child processes of a windowless (pythonw / scheduled) run would each flash a console
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run that decodes text as UTF-8 on every OS and never opens a console window."""
    if kwargs.get("text") or isinstance(kwargs.get("input"), str):
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    return subprocess.run(cmd, **_NO_WINDOW, **kwargs)


# ── File lock ──────────────────────────────────────────────────────────────────

if IS_WINDOWS:
    import msvcrt

    def _lock(f) -> None:
        f.seek(0)
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)   # gives up after ~10 s, so keep trying
                return
            except OSError:
                time.sleep(0.1)

    def _unlock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f) -> None:
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f) -> None:
        fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path: Path):
    """Block until this process holds the exclusive lock on `path` (created if missing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        _lock(f)
        try:
            yield
        finally:
            _unlock(f)


def replace(src: Path, dst: Path) -> None:
    """os.replace, retried briefly: on Windows it fails while another process has dst open."""
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1)


# ── Notifications ──────────────────────────────────────────────────────────────

# Windows PowerShell's own AppUserModelID: toasts shown under it need no app registration.
# (PowerShell 7 can't load the WinRT types, so this always runs powershell.exe.)
_PS_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_PS_TOAST = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:TIMELOG_TOAST)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_PS_APP_ID}').Show($toast)
"""


def toast_xml(title: str, message: str, url: str) -> str:
    """A Windows toast that opens `url` when clicked."""
    from xml.sax.saxutils import escape, quoteattr   # here, not at the top: it costs the hook ~35 ms
    return (f'<toast activationType="protocol" launch={quoteattr(url)}><visual><binding template="ToastGeneric">'
            f"<text>{escape(title)}</text><text>{escape(message)}</text></binding></visual></toast>")


def notify(title: str, message: str, url: str) -> None:
    """Best effort: a missing notifier or a failure never stops the caller."""
    try:
        if IS_MAC:
            notifier = shutil.which("terminal-notifier") or next(
                (p for p in ("/usr/local/bin/terminal-notifier", "/opt/homebrew/bin/terminal-notifier")
                 if Path(p).exists()), None)
            if notifier:   # a click opens the URL
                run([notifier, "-title", title, "-message", message, "-open", url, "-group", "timelog"],
                    capture_output=True)
            else:          # osascript can't open anything on click, so the URL goes in the text
                run(["osascript", "-e", "on run argv", "-e",
                     "display notification (item 1 of argv) with title (item 2 of argv)",
                     "-e", "end run", f"{message} — open {url}", title], capture_output=True)
        elif IS_WINDOWS:
            run(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", "-"],
                input=_PS_TOAST, capture_output=True, timeout=30,
                env={**os.environ, "TIMELOG_TOAST": toast_xml(title, message, url)})
        elif shutil.which("notify-send"):
            run(["notify-send", "--app-name", title, title, f"{message} — {url}"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
