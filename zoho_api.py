"""Zoho People Timetracker API: the one place that talks to Zoho.

Config, auth (cached token), reads (projects, jobs, time logs), the job catalog (which jobs
are open on a day), adding a time log, and time helpers. Used by the draft job (drafts.py)
and the review app.
"""

import datetime
import os
import re
import sys
import time

import requests

from constants import CATALOG_TTL, TOKEN_TTL, ZOHO_API, ZOHO_DOMAIN, ZOHO_LOG_FILE, ZOHO_TIMEOUT

# Credentials and identity (constants has loaded .env)
CLIENT_ID     = os.getenv("ZOHO_CLIENT_ID")
CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
USER          = os.getenv("ZOHO_USER")


class ZohoAPIError(RuntimeError):
    """Raised by strict API reads when Zoho returns an error response."""


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

_SECRET_RE = re.compile(r"((?:refresh_token|client_secret|client_id|access_token|code)=)[^&\s'\"]+")
_token_cache: dict = {"at": 0.0, "token": None}


def _redact(text) -> str:
    """Mask credentials in anything we print (exception texts can contain request URLs)."""
    text = _SECRET_RE.sub(r"\1***", str(text))
    for secret in (REFRESH_TOKEN, CLIENT_SECRET, CLIENT_ID):
        if secret:
            text = text.replace(secret, "***")
    return text


def get_access_token() -> str | None:
    """A fresh access token from the refresh token, or None (reason printed to stderr)."""
    # Credentials go in the POST body, never the URL: URLs end up in exception texts and logs.
    body = {"refresh_token": REFRESH_TOKEN, "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET, "grant_type": "refresh_token"}
    try:
        data = requests.post(f"https://accounts.{ZOHO_DOMAIN}/oauth/v2/token", data=body,
                             timeout=ZOHO_TIMEOUT).json()
    except Exception as e:
        print(f"Token fetch failed: {type(e).__name__}: {_redact(e)}", file=sys.stderr)
        return None
    if not data.get("access_token"):
        print(f"Token error: {_redact(data.get('error', data))}", file=sys.stderr)
    return data.get("access_token")


def token() -> str:
    """Cached access token; raises RuntimeError if Zoho won't give one."""
    if _token_cache["token"] and time.time() - _token_cache["at"] < TOKEN_TTL:
        return _token_cache["token"]
    tok = get_access_token()
    if not tok:
        raise RuntimeError("Zoho auth failed: check the refresh token in .env")
    _token_cache.update(at=time.time(), token=tok)
    return tok


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
    params = {"user": USER, "fromDate": from_date.isoformat(), "toDate": to_date.isoformat(),
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
        with open(ZOHO_LOG_FILE, "a") as f:
            f.write(f"{work_date.isoformat()} | {what} | {when} | {description}\n")
    except OSError:
        pass


def add_timelog(job_id: str, work_date: datetime.date, description: str, tok: str, *, hours: str,
                project_id: str = "", billing: str = "Billable", label: str = "") -> dict:
    """Create an hours-mode time log; hours is 'HH:MM'."""
    data = {"user": USER, "jobId": str(job_id), "workDate": work_date.isoformat(), "billingStatus": billing,
            "workItem": description, "hours": hours}
    if project_id:
        data["projectId"] = str(project_id)
    res = _post("addtimelog", data, tok)
    if res["success"]:
        _log_locally(work_date, label or str(job_id), hours, description)
    return res
