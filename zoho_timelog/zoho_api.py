"""Zoho People Timetracker API: the one place that talks to Zoho.

Auth (your own Self Client, cached token), reads (projects, jobs, time logs), the job catalog
(which jobs are open on a day), adding a time log, and time helpers. Used by the draft job
(drafts.py) and the review app.
"""

import datetime
import json
import os
import re
import sys
import time

import requests

from . import oscompat
from .constants import (CATALOG_TTL, NOTE_CHARS, TOKEN_TTL, ZOHO_ACCOUNTS, ZOHO_API, ZOHO_DOMAIN, ZOHO_LOG_FILE,
                        ZOHO_TIMEOUT, ZOHO_TOKEN_FILE)


class ZohoAPIError(RuntimeError):
    """Raised by strict API reads when Zoho returns an error response."""


class NotConnected(RuntimeError):
    """No Zoho credentials yet: Connect Zoho in the review app (or `timelog login`) sets them up."""


# ── Time helpers ───────────────────────────────────────────────────────────────

def hhmm_to_minutes(t_str: str) -> int:
    hours, minutes = t_str.split(":")
    return int(hours) * 60 + int(minutes)


def is_ranged(log: dict) -> bool:
    """True for a start–end log. Hours-mode logs come back with no times, or 00:00–00:00."""
    start, end = log.get("fromTime"), log.get("toTime")
    return isinstance(start, int) and isinstance(end, int) and end > start


def log_minutes(log: dict) -> int:
    """Minutes a time log covers: hoursInMins, else toTime − fromTime, else 'HH:MM' hours."""
    if log.get("hoursInMins") not in (None, ""):
        try:
            return int(float(log["hoursInMins"]))
        except (TypeError, ValueError):
            pass
    if is_ranged(log):
        return log["toTime"] - log["fromTime"]
    hours = str(log.get("hours") or "")
    if ":" in hours:
        try:
            return hhmm_to_minutes(hours)
        except ValueError:
            pass
    return 0


# ── Auth ───────────────────────────────────────────────────────────────────────
#
# Everyone uses their own Zoho Self Client (api-console.zoho.in). Connect Zoho in the review app
# exchanges its Client ID, Client Secret and a one-time code for a refresh token and saves them
# in ~/.timelog/zoho_token.json. ZOHO_* values in the environment, config.env or a checkout's .env
# take precedence (developer setups).

_SECRET_RE = re.compile(r"((?:refresh_token|client_secret|client_id|access_token|code)=)[^&\s'\"]+")
_token_cache: dict = {"at": 0.0, "token": None}
NOT_CONNECTED = "Zoho isn't connected: open the review app and click Connect Zoho (or run `timelog login`)"


def credentials() -> dict:
    """{client_id, client_secret, refresh_token, user, accounts_server, source: "env"|"file"}.
    Read on every call, so a connection made in the app applies at once. Raises NotConnected."""
    if os.getenv("ZOHO_REFRESH_TOKEN"):
        return {"client_id": os.getenv("ZOHO_CLIENT_ID"), "client_secret": os.getenv("ZOHO_CLIENT_SECRET"),
                "refresh_token": os.getenv("ZOHO_REFRESH_TOKEN"), "user": os.getenv("ZOHO_USER"),
                "accounts_server": ZOHO_ACCOUNTS, "source": "env"}
    try:
        saved = json.loads(ZOHO_TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise NotConnected(NOT_CONNECTED) from None
    if not saved.get("refresh_token"):
        raise NotConnected(NOT_CONNECTED)
    return {"accounts_server": ZOHO_ACCOUNTS, **saved, "source": "file"}


def user() -> str | None:
    """Your Zoho email, which the People API calls `user`."""
    return credentials().get("user")


def auth_status() -> dict:
    try:
        creds = credentials()
    except NotConnected:
        return {"connected": False}
    return {"connected": True, "user": creds.get("user"), "source": creds["source"]}


def _redact(text) -> str:
    """Mask credentials in anything we print (exception texts can contain request URLs)."""
    text = _SECRET_RE.sub(r"\1***", str(text))
    try:
        creds = credentials()
    except NotConnected:
        return text
    for key in ("refresh_token", "client_secret", "client_id"):
        if creds.get(key):
            text = text.replace(creds[key], "***")
    return text


def get_access_token() -> str | None:
    """A fresh access token from the refresh token, or None (reason printed to stderr)."""
    creds = credentials()
    # Credentials go in the POST body, never the URL: URLs end up in exception texts and logs.
    body = {"refresh_token": creds["refresh_token"], "client_id": creds["client_id"],
            "client_secret": creds["client_secret"], "grant_type": "refresh_token"}
    try:
        data = requests.post(f"{creds['accounts_server']}/oauth/v2/token", data=body,
                             timeout=ZOHO_TIMEOUT).json()
    except Exception as e:
        print(f"Token fetch failed: {type(e).__name__}: {_redact(e)}", file=sys.stderr)
        return None
    if not data.get("access_token"):
        print(f"Token error: {_redact(data.get('error', data))}", file=sys.stderr)
    return data.get("access_token")


def token() -> str:
    """Cached access token. Raises NotConnected without credentials, RuntimeError if Zoho refuses them."""
    if _token_cache["token"] and time.time() - _token_cache["at"] < TOKEN_TTL:
        return _token_cache["token"]
    tok = get_access_token()
    if not tok:
        where = ("the ZOHO_* values in your environment, config.env or .env" if credentials()["source"] == "env"
                 else "your connection: Disconnect and Connect Zoho again in the review app")
        raise RuntimeError(f"Zoho auth failed: check {where}")
    _token_cache.update(at=time.time(), token=tok)
    return tok


_CONNECT_ERRORS = {
    "invalid_code": "That code has expired or was already used. Generate a new one (Self Client → Generate Code) "
                    "and paste it again.",
    "invalid_client": "Zoho doesn't recognise that Client ID. Copy it again from Self Client → Client Secret.",
    "invalid_client_secret": "That Client Secret doesn't match the Client ID. Copy both again from Self Client → "
                             "Client Secret.",
}


def connect(client_id: str, client_secret: str, code: str, email: str = "") -> str:
    """Exchange your Self Client's one-time code for a refresh token and save the connection.
    Returns your Zoho email. ValueError (with what to do) for anything you can fix."""
    client_id, client_secret, code, email = (v.strip() for v in (client_id, client_secret, code, email or ""))
    if not (client_id and client_secret and code):
        raise ValueError("Paste the Client ID, the Client Secret and the code")
    body = {"grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret, "code": code}
    data = requests.post(f"{ZOHO_ACCOUNTS}/oauth/v2/token", data=body, timeout=ZOHO_TIMEOUT).json()
    if data.get("error") or not data.get("refresh_token"):
        error = data.get("error") or "no refresh token in the reply"
        raise ValueError(_CONNECT_ERRORS.get(error, f"Zoho refused the code ({error}). Generate a new one and try again."))
    if not email:
        try:
            info = requests.get(f"{ZOHO_ACCOUNTS}/oauth/user/info", timeout=ZOHO_TIMEOUT,
                                headers={"Authorization": f"Zoho-oauthtoken {data['access_token']}"}).json()
            email = info.get("Email") or info.get("email") or ""
        except (requests.RequestException, ValueError):
            email = ""
    if not email:
        raise ValueError("Zoho didn't share your email (is AaaServer.profile.Read in the code's scope?). Type your "
                         "Zoho email in the form and connect again with a new code.")
    saved = {"client_id": client_id, "client_secret": client_secret, "refresh_token": data["refresh_token"],
             "user": email, "accounts_server": ZOHO_ACCOUNTS}
    ZOHO_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ZOHO_TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)   # only you can read it (Windows: your profile folder is already private)
    oscompat.replace(tmp, ZOHO_TOKEN_FILE)
    _token_cache.update(at=time.time(), token=data["access_token"])
    return email


def disconnect() -> bool:
    """Forget the saved connection. False if there was none (or the credentials come from env/.env)."""
    _token_cache.update(at=0.0, token=None)
    if ZOHO_TOKEN_FILE.exists():
        ZOHO_TOKEN_FILE.unlink()
        return True
    return False


# ── Reads ──────────────────────────────────────────────────────────────────────

def fetch_data(endpoint: str, tok: str) -> list:
    """GET a timetracker list endpoint (getprojects, getjobs); [] on any error."""
    try:
        r = requests.get(f"{ZOHO_API}/{endpoint}", headers={"Authorization": f"Zoho-oauthtoken {tok}"},
                         timeout=ZOHO_TIMEOUT)
        result = r.json().get("response", {}).get("result", [])
    except Exception as e:
        print(f"{endpoint} failed: {type(e).__name__}: {_redact(e)}", file=sys.stderr)
        return []
    return result if isinstance(result, list) else []


def _normalize_timelog(log: dict) -> dict:
    """fromTime/toTime as minutes since midnight (None for hours-mode logs), workItem → taskName."""
    for key in ("fromTime", "toTime"):
        val = log.get(key)
        if isinstance(val, str):
            try:
                log[key] = hhmm_to_minutes(val) if ":" in val else int(val)
            except ValueError:
                log[key] = None
    if "taskName" not in log and "workItem" in log:
        log["taskName"] = log["workItem"]
    return log


def fetch_existing_logs(work_date: datetime.date, tok: str, strict: bool = False) -> list:
    """One day's time logs.

    With strict=True a Zoho error raises ZohoAPIError instead of returning [],
    so callers that dedup against these logs never mistake an error for "no logs".
    """
    return fetch_logs_range(work_date, work_date, tok, strict)


def fetch_logs_range(from_date: datetime.date, to_date: datetime.date, tok: str, strict: bool = False) -> list:
    """Time logs from from_date to to_date inclusive; each has workDate as YYYY-MM-DD."""
    params = {"user": user(), "fromDate": from_date.isoformat(), "toDate": to_date.isoformat(),
              "dateFormat": "yyyy-MM-dd"}
    try:
        r = requests.get(f"{ZOHO_API}/gettimelogs", params=params, timeout=ZOHO_TIMEOUT,
                         headers={"Authorization": f"Zoho-oauthtoken {tok}"}).json().get("response", {})
    except Exception as e:
        if strict:
            raise ZohoAPIError(f"gettimelogs request failed: {type(e).__name__}: {_redact(e)}") from e
        print(f"  gettimelogs failed: {_redact(e)}", file=sys.stderr)
        return []
    if str(r.get("status")) != "0":
        msg = f"gettimelogs error: {r.get('errors') or r.get('message')}"
        if strict:
            raise ZohoAPIError(msg)
        print(f"  {msg}", file=sys.stderr)
        return []
    result = r.get("result", [])
    return [_normalize_timelog(log) for log in result] if isinstance(result, list) else []


# ── Catalog: projects and the jobs you can log to ──────────────────────────────

_catalog_cache: dict = {"at": 0.0, "data": None}


def _iso(value) -> str | None:
    try:
        return datetime.date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def catalog(fresh: bool = False) -> list[dict]:
    """All projects with all their jobs, cached CATALOG_TTL:
    [{"id", "name", "status", "jobs": [{"id", "name", "status", "active", "from", "to"}]}].
    Use open_catalog() for what can be logged on a given day."""
    if not fresh and _catalog_cache["data"] and time.time() - _catalog_cache["at"] < CATALOG_TTL:
        return _catalog_cache["data"]
    tok = token()
    projects, jobs = fetch_data("getprojects", tok), fetch_data("getjobs", tok)
    if not projects:
        raise RuntimeError("Zoho returned no projects")
    data = [{"id": str(p["projectId"]), "name": p["projectName"].strip(), "status": p.get("projectStatus") or "",
             "jobs": [{"id": str(j["jobId"]), "name": j["jobName"].strip(), "status": j.get("jobStatus") or "",
                       "active": j.get("isActive") is not False, "from": _iso(j.get("fromDate")),
                       "to": _iso(j.get("toDate"))}
                      for j in jobs if str(j.get("projectId")) == str(p["projectId"])]}
            for p in projects]
    data.sort(key=lambda p: p["name"].lower())
    _catalog_cache.update(at=time.time(), data=data)
    return data


def _closed(status: str) -> bool:
    return any(w in (status or "").lower() for w in ("complet", "close", "cancel"))


def job_open(job: dict, day: datetime.date) -> bool:
    """A job you can log to on `day`: not completed, active, and within its from/to dates (if set)."""
    iso = day.isoformat()
    return (job.get("active", True) and not _closed(job.get("status", ""))
            and (not job.get("from") or job["from"] <= iso) and (not job.get("to") or iso <= job["to"]))


def open_catalog(projects: list[dict], day: datetime.date) -> list[dict]:
    """Only open projects and their jobs that are open on `day`; projects left without jobs are dropped."""
    out = []
    for p in projects:
        jobs = [j for j in p["jobs"] if job_open(j, day)]
        if jobs and not _closed(p.get("status", "")):
            out.append({**p, "jobs": jobs})
    return out


def find_project(projects: list[dict], name: str = "", pid: str = "") -> dict | None:
    for p in projects:
        if (pid and p["id"] == str(pid)) or (name and p["name"].strip().lower() == name.strip().lower()):
            return p
    return None


def find_job(project: dict | None, name: str = "", jid: str = "") -> dict | None:
    for j in (project or {}).get("jobs", []):
        if (jid and j["id"] == str(jid)) or (name and j["name"].strip().lower() == name.strip().lower()):
            return j
    return None


# ── Leave and holidays ─────────────────────────────────────────────────────────

LEAVE_API    = f"https://people.{ZOHO_DOMAIN}/api/v2/leavetracker/leaves/records"
HOLIDAYS_API = f"https://people.{ZOHO_DOMAIN}/people/api/leave/v2/holidays/get"
_days_off_cache: dict = {}   # (from, to) → (fetched at, result)


def _zoho_date(text) -> str | None:
    """'08-May-2024' (or '2024-05-08') → '2024-05-08'."""
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(text).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _api_error(what: str, body) -> "ZohoAPIError":
    """Zoho's error, e.g. {"response": {"errors": {"code": 7218, "message": "Invalid OAuth Scope."}}}, readable."""
    errors = (body.get("response") or body).get("errors") if isinstance(body, dict) else None
    if isinstance(errors, dict) and errors.get("code") == 7218:
        return ZohoAPIError("your Zoho connection has no leave access")
    text = errors.get("message") if isinstance(errors, dict) else (body.get("message") if isinstance(body, dict) else None)
    return ZohoAPIError(f"{what}: {str(text or body)[:NOTE_CHARS]}")


def _leave_fix() -> str:
    try:
        from_env = credentials()["source"] == "env"
    except NotConnected:
        from_env = False
    if from_env:
        return ("Your ZOHO_* values come from env/config.env/.env: make a new Self Client code with the scope in "
                "SETUP_GUIDE.md and update ZOHO_REFRESH_TOKEN, or remove them and use Connect Zoho.")
    return "To fix it, Disconnect and Connect Zoho again in the review app: a new code includes leave access."


def days_off(from_date: datetime.date, to_date: datetime.date, tok: str) -> tuple[dict[str, str], str | None]:
    """Your full days off in Zoho, {"YYYY-MM-DD": "leave" | "holiday"}, plus a note if they couldn't be read.

    Never raises: the missed-day check works without them. Counts approved full-day leave only (a half day
    or hour-based leave still leaves time to log) and full, non-optional holidays (an optional holiday
    you take shows up as leave). Cached CATALOG_TTL: both APIs allow only 30 calls per 5 minutes.
    """
    key = (from_date, to_date)
    hit = _days_off_cache.get(key)
    if hit and time.time() - hit[0] < CATALOG_TTL:
        return hit[1]
    dates = {"from": from_date.strftime("%d-%b-%Y"), "to": to_date.strftime("%d-%b-%Y"), "dateFormat": "dd-MMM-yyyy"}
    headers = {"Authorization": f"Zoho-oauthtoken {tok}"}
    off: dict[str, str] = {}
    try:
        leave = requests.get(LEAVE_API, headers=headers, timeout=ZOHO_TIMEOUT, params={
            **dates, "approvalStatus": '["APPROVED"]', "dataSelect": "MINE", "limit": 200}).json()
        if not isinstance(leave.get("records"), dict):
            raise _api_error("leave", leave)
        for record in leave["records"].values():
            if record.get("Unit") != "Days" or str(record.get("ApprovalStatus", "")).lower() != "approved":
                continue
            for day, info in (record.get("Days") or {}).items():
                if float((info or {}).get("LeaveCount") or 0) >= 1 and _zoho_date(day):
                    off[_zoho_date(day)] = "leave"

        holidays = requests.get(HOLIDAYS_API, params=dates, headers=headers, timeout=ZOHO_TIMEOUT).json()
        if not isinstance(holidays.get("data"), list):
            raise _api_error("holidays", holidays)
        for holiday in holidays["data"]:
            if not holiday.get("isHalfday") and not holiday.get("isRestrictedHoliday") and _zoho_date(holiday.get("Date")):
                off[_zoho_date(holiday["Date"])] = "holiday"
        result = (off, None)
    except (requests.RequestException, ValueError, TypeError, AttributeError, ZohoAPIError) as exc:
        result = ({}, f"Leave and holidays not read from Zoho ({_redact(exc)[:NOTE_CHARS]}). {_leave_fix()}")
    _days_off_cache[key] = (time.time(), result)
    return result


# ── Writes ─────────────────────────────────────────────────────────────────────

def _post(endpoint: str, data: dict, tok: str) -> dict:
    """POST to a timetracker endpoint → {"success", "log_id", "error", "code"}."""
    try:
        res = requests.post(f"{ZOHO_API}/{endpoint}", data=data, timeout=ZOHO_TIMEOUT,
                            headers={"Authorization": f"Zoho-oauthtoken {tok}"}).json()
    except Exception as e:
        return {"success": False, "log_id": None, "error": f"network error: {type(e).__name__}", "code": None}
    r = res.get("response", {}) if isinstance(res, dict) else {}
    if str(r.get("status")) == "0":
        result = r.get("result")
        first = result[0] if isinstance(result, list) and result else result if isinstance(result, dict) else {}
        return {"success": True, "log_id": (first or {}).get("timeLogId"), "error": None, "code": None}
    errors = r.get("errors") or []
    errors = [errors] if isinstance(errors, dict) else errors
    message = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errors) or r.get("message") or "unknown error"
    return {"success": False, "log_id": None, "error": message, "code": errors[0].get("code") if errors else None}


def _log_locally(work_date: datetime.date, what: str, when: str, description: str) -> None:
    try:
        ZOHO_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ZOHO_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{work_date.isoformat()} | {what} | {when} | {description}\n")
    except OSError:
        pass


def add_timelog(job_id: str, work_date: datetime.date, description: str, tok: str, *, hours: str,
                project_id: str = "", billing: str = "Billable", label: str = "") -> dict:
    """Create an hours-mode time log; hours is 'HH:MM'."""
    data = {"user": user(), "jobId": str(job_id), "workDate": work_date.isoformat(), "billingStatus": billing,
            "workItem": description, "hours": hours}
    if project_id:
        data["projectId"] = str(project_id)
    res = _post("addtimelog", data, tok)
    if res["success"]:
        _log_locally(work_date, label or str(job_id), hours, description)
    return res
