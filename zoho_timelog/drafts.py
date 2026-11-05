"""Daily time-log drafts: combine a day's activity, review it, then log it to Zoho.

    generate_day(day)   sources → minute timeline → one draft per Zoho project+job
                        (Claude picks 'auto' jobs and writes descriptions)
                        → ~/.timelog/drafts/DATE.json, nothing sent to Zoho
    submit(day, id)     logs one accepted draft (only the time Zoho doesn't already have)
    missed_days(...)    weekdays with nothing in Zoho → notify()

The review app (review_app.py) and the CLI (cli.py) are thin layers over these.
"""

import contextlib
import datetime
import difflib
import json
import math
import re
import time
import uuid
from pathlib import Path

from . import oscompat
from . import zoho_api as zoho
from .constants import (APP_URL, BACKLOG_DAYS, DAY_MINUTES, DAYS_FILE, DRAFTS_DIR, ERROR_CHARS, GAP_MIN,
                       GEN_LOCK, GEN_LOCK_STALE, MARKS, MAX_PROMPTS, MISSED_WINDOW, NOTE_CHARS, OPEN,
                       PROMPT_FILE, ROUND_MIN, RUN_LOG, TAIL_MIN, TIMELOG_DIR)
from .sources import (CLAUDE_ERRORS, day_bounds, git_commits, load_activity, load_mapping, outlook_meetings,
                     run_claude)
from .zoho_api import find_job, find_project, log_minutes, open_catalog


# ── Time math (pure) ───────────────────────────────────────────────────────────

def block_minutes(times: list[datetime.datetime]) -> float:
    """Total minutes of work blocks: split at gaps > GAP_MIN, each block = span + TAIL_MIN."""
    return sum((hi - lo).total_seconds() / 60 for lo, hi in _blocks(times))


def _blocks(times: list[datetime.datetime]) -> list[tuple[datetime.datetime, datetime.datetime]]:
    if not times:
        return []
    times = sorted(times)
    tail = datetime.timedelta(minutes=TAIL_MIN)
    blocks, start, prev = [], times[0], times[0]
    for t in times[1:]:
        if (t - prev).total_seconds() > GAP_MIN * 60:
            blocks.append((start, prev + tail))
            start = t
        prev = t
    blocks.append((start, prev + tail))
    return blocks


def minute_range(lo: datetime.datetime, hi: datetime.datetime, day_start: datetime.datetime) -> range:
    """Minute-of-day slots covered by [lo, hi), clipped to the day."""
    a = math.floor((lo - day_start).total_seconds() / 60)
    b = math.ceil((hi - day_start).total_seconds() / 60)
    return range(max(0, a), min(DAY_MINUTES, b))


def round_minutes(minutes: float) -> int:
    """Round to the nearest ROUND_MIN, halves up (7 → 0, 8 → 15, 52 → 45, 53 → 60)."""
    return int(math.floor(minutes / ROUND_MIN + 0.5) * ROUND_MIN)


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_hhmm(text: str) -> int:
    """'2:30' or '02:30' → 150; '1.5' → 90. Raises ValueError."""
    text = str(text).strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        h, m = text.split(":")
        if int(m) >= 60:
            raise ValueError("minutes must be under 60")
        return int(h) * 60 + int(m)
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return round(float(text) * 60)
    raise ValueError("use HH:MM, e.g. 02:30")


# ── Mapping (pure) ─────────────────────────────────────────────────────────────

def resolve_mapping(repo: str, group: str, mapping: dict) -> tuple[str, dict | None]:
    """Map a repo to {"project", "job"}: ignore list, then exact repo, then longest group prefix."""
    if repo in mapping.get("ignore", []):
        return "ignored", None
    if repo in mapping.get("repos", {}):
        return "mapped", mapping["repos"][repo]
    best = None
    for prefix, target in mapping.get("groups", {}).items():
        if group == prefix or group.startswith(prefix + "/"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, target)
    if best:
        return "mapped", best[1]
    return "unmapped", None


def match_meeting(subject: str, mapping: dict) -> tuple[str, dict | None]:
    """ignore_meetings substrings, then the first `meetings` rule whose match is in the subject."""
    low = subject.lower()
    if any(s.lower() in low for s in mapping.get("ignore_meetings", [])):
        return "ignored", None
    for rule in mapping.get("meetings", []):
        if rule.get("match") and rule["match"].lower() in low:
            return "mapped", rule
    return "unmapped", None


# ── Timeline (pure) ────────────────────────────────────────────────────────────

def build_timeline(day: datetime.date, records: list[dict], meetings: list[dict], mapping: dict) -> dict:
    """Give each minute of the day to one owner, meetings first, then Claude Code blocks.

    Returns {"groups": [...], "ignored": [...]}. A group is a mapped project+job ("mapped") or
    something Claude has to classify: an unmatched meeting ("meeting") or unmapped repo ("repo").
    """
    day_start, _ = day_bounds(day)
    slots: list[tuple[str, str] | None] = [None] * DAY_MINUTES
    groups: dict[str, dict] = {}
    ignored = []

    def group(key: str, **info) -> dict:
        return groups.setdefault(key, {"key": key, **info, "raw": 0, "sources": {}, "repos": [], "meetings": []})

    def mapped_group(target: dict) -> dict:
        job = target.get("job") or "auto"
        return group(f"mapped|{target['project']}|{job}", kind="mapped", project=target["project"], config_job=job)

    def claim(rng: range, key: str, label: str) -> None:
        for i in rng:
            if slots[i] is None:
                slots[i] = (key, label)

    for m in meetings:
        state, target = match_meeting(m["subject"], mapping)
        rng = minute_range(m["start"], m["end"], day_start)
        if state == "ignored":
            ignored.append({"label": m["subject"], "kind": "meeting", "minutes": len(rng), "reason": "ignored"})
            continue
        if state == "mapped":
            g = mapped_group(target)
        else:
            g = group(f"meeting|{m['subject']}", kind="meeting", label=m["subject"])
        g["meetings"].append(f"{m['start']:%H:%M}–{m['end']:%H:%M} {m['subject']}")
        claim(rng, g["key"], "Meeting")

    by_repo: dict[str, list[dict]] = {}
    for rec in records:
        by_repo.setdefault(rec["repo"], []).append(rec)
    for repo, recs in sorted(by_repo.items()):
        group_path = next((r.get("remote_group") for r in recs if r.get("remote_group")), "")
        blocks = _blocks([r["dt"] for r in recs])
        state, target = resolve_mapping(repo, group_path, mapping)
        if state == "ignored":
            ignored.append({"label": repo, "kind": "repo", "reason": "ignored",
                            "minutes": round(sum((b - a).total_seconds() for a, b in blocks) / 60)})
            continue
        if state == "mapped":
            g = mapped_group(target)
        else:
            g = group(f"repo|{repo}", kind="repo", label=repo, group=group_path)
        g["repos"].append({
            "name": repo,
            "path": next((r.get("repo_path") for r in recs if r.get("repo_path")), ""),
            "branches": sorted({r["branch"] for r in recs if r.get("branch")}),
            "prompts": list(dict.fromkeys(r.get("prompt", "") for r in recs if r.get("prompt")))[:MAX_PROMPTS],
        })
        for a, b in blocks:
            claim(minute_range(a, b, day_start), g["key"], "Claude Code")

    for slot in slots:
        if slot:
            g = groups[slot[0]]
            g["raw"] += 1
            g["sources"][slot[1]] = g["sources"].get(slot[1], 0) + 1
    return {"groups": [g for g in groups.values() if g["raw"] > 0], "ignored": ignored}


# ── Drafts from a timeline (pure) ──────────────────────────────────────────────

def new_draft(**fields) -> dict:
    d = {"id": uuid.uuid4().hex[:8], "project": "", "project_id": None, "job": "", "job_id": None,
         "minutes": 0, "hours": "00:00", "description": "", "sources": {}, "items": [],
         "suggested": False, "suggested_minutes": 0, "zoho_minutes": 0, "status": "pending",
         "error": None, "edited": False, "manual": False, "log_id": None, "submitted_at": None}
    d.update(fields)
    d["hours"] = hhmm(d["minutes"])
    return d


def similar(new: str, existing: str) -> bool:
    """True if `new` adds nothing: already contained, or a near-copy of one of the parts."""
    new = new.strip().lower()
    parts = [p.strip().lower() for p in existing.split(";") if p.strip()]
    return not new or any(new in p or difflib.SequenceMatcher(None, new, p).ratio() > 0.75 for p in parts)


def zoho_minutes(logs: list[dict], job_id: str) -> int:
    """Minutes Zoho already has for a job in these logs."""
    return sum(log_minutes(log) for log in logs if str(log.get("jobId")) == str(job_id))


def group_items(g: dict) -> list[str]:
    items = [f"repo {r['name']}" + (f" ({', '.join(r['branches'])})" if r["branches"] else "") for r in g["repos"]]
    return items + [f"meeting {m}" for m in g["meetings"]]


def build_drafts(timeline: dict, catalog: list[dict], answers: dict, zoho_logs: list[dict]) -> tuple[list, list]:
    """Turn timeline groups into drafts (merged per Zoho job) plus a not-counted list.

    answers: {"entries": {gid: {job, description}}, "buckets": {gid: {personal, project, job, description}}},
    keyed by the ids given to Claude (the group's position, see claude_payload).
    """

    merged: dict[str, dict] = {}
    not_counted = list(timeline["ignored"])
    # Mapped work first, so its description leads when a suggested bucket merges into the same job.
    for gid, g in sorted(enumerate_groups(timeline), key=lambda x: x[1]["kind"] != "mapped"):
        suggested = g["kind"] != "mapped"
        if suggested:
            ans = answers.get("buckets", {}).get(gid, {})
            if ans.get("personal") or not ans.get("project"):
                not_counted.append({"label": g["label"], "kind": g["kind"], "minutes": g["raw"],
                                    "reason": "personal" if ans.get("personal") else "unclassified"})
                continue
            project_name, job_name = ans["project"], ans.get("job") or ""
        else:
            ans = answers.get("entries", {}).get(gid, {})
            project_name = g["project"]
            job_name = g["config_job"] if g["config_job"] != "auto" else (ans.get("job") or "")

        project = find_project(catalog, name=project_name)
        job = find_job(project, name=job_name)
        error = None
        if not project:
            error = f"Project '{project_name}' not found in Zoho"
        elif not job:
            error = "Pick a job" + (f" ('{job_name}' not found)" if job_name else "")
        key = job["id"] if job else f"nojob|{project_name}|{gid}"
        d = merged.get(key)
        if d is None:
            d = merged[key] = new_draft(
                project=project["name"] if project else project_name, project_id=project and project["id"],
                job=job["name"] if job else job_name, job_id=job and job["id"],
                description=(ans.get("description") or "").strip(), error=error, raw=0)
        elif ans.get("description") and not similar(ans["description"], d["description"]):
            d["description"] = "; ".join(x for x in (d["description"], ans["description"].strip()) if x)
        d["raw"] += g["raw"]
        for label, mins in g["sources"].items():
            d["sources"][label] = d["sources"].get(label, 0) + mins
        d["items"] += group_items(g)
        if suggested:
            d["suggested"] = True
            d["suggested_minutes"] += g["raw"]

    drafts = []
    for d in merged.values():
        raw = d.pop("raw")
        d["minutes"] = round_minutes(raw)
        d["hours"] = hhmm(d["minutes"])
        if d["minutes"] == 0:
            not_counted.append({"label": f"{d['project']} / {d['job'] or '?'}", "kind": "short",
                                "minutes": raw, "reason": f"under {ROUND_MIN // 2 + 1} minutes"})
            continue
        d["zoho_minutes"] = zoho_minutes(zoho_logs, d["job_id"]) if d["job_id"] else 0
        if d["job_id"] and d["zoho_minutes"] >= d["minutes"]:
            d["status"] = "in_zoho"
        drafts.append(d)
    for n in not_counted:
        n.setdefault("id", uuid.uuid4().hex[:8])
    drafts.sort(key=lambda d: (d["project"].lower(), d["job"].lower()))
    return drafts, not_counted


def enumerate_groups(timeline: dict):
    """Stable ids for groups: e1, e2… for mapped entries, b1, b2… for buckets."""
    e = b = 0
    for g in timeline["groups"]:
        if g["kind"] == "mapped":
            e += 1
            yield f"e{e}", g
        else:
            b += 1
            yield f"b{b}", g


# ── Claude: jobs, descriptions, suggestions ────────────────────────────────────

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "job": {"type": ["string", "null"]},
                           "description": {"type": "string"}},
            "required": ["id", "job", "description"],
        }},
        "buckets": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "personal": {"type": "boolean"},
                           "project": {"type": ["string", "null"]}, "job": {"type": ["string", "null"]},
                           "description": {"type": "string"}},
            "required": ["id", "personal", "project", "job", "description"],
        }},
    },
    "required": ["entries", "buckets"],
}


def claude_payload(day: datetime.date, timeline: dict, catalog: list[dict]) -> dict:
    catalog = open_catalog(catalog, day)  # Claude only chooses jobs that can be logged that day
    entries, buckets = [], []
    for gid, g in enumerate_groups(timeline):
        repos = [{"name": r["name"], "branches": r["branches"], "prompts": r["prompts"],
                  "commits": [c["subject"] for c in git_commits(r["path"], day)]} for r in g["repos"]]
        if g["kind"] == "mapped":
            project = find_project(catalog, name=g["project"])
            entries.append({"id": gid, "project": g["project"],
                            "pinned_job": None if g["config_job"] == "auto" else g["config_job"],
                            "candidate_jobs": [j["name"] for j in (project or {}).get("jobs", [])],
                            "hours": hhmm(g["raw"]), "repos": repos, "meetings": g["meetings"]})
        else:
            buckets.append({"id": gid, "kind": g["kind"], "label": g["label"], "hours": hhmm(g["raw"]),
                            "group": g.get("group", ""), "repos": repos, "meetings": g["meetings"]})
    return {"date": day.isoformat(), "month": day.strftime("%b"), "entries": entries, "buckets": buckets,
            "projects": [{"name": p["name"], "jobs": [j["name"] for j in p["jobs"]]} for p in catalog]}


def ask_claude(payload: dict) -> dict:
    """Headless Claude, no tools and no MCP servers. Returns answers keyed by id."""
    prompt = PROMPT_FILE.read_text(encoding="utf-8") +"\n\n```json\n" + json.dumps(payload, indent=1) + "\n```\n"
    reply = run_claude(prompt, DRAFT_SCHEMA)
    return {"entries": {a["id"]: a for a in reply.get("entries", [])},
            "buckets": {a["id"]: a for a in reply.get("buckets", [])}}


# ── Store: ~/.timelog/drafts/DATE.json ─────────────────────────────────────────

def day_path(day: datetime.date) -> Path:
    return DRAFTS_DIR / f"{day.isoformat()}.json"


def load_day(day: datetime.date) -> dict | None:
    path = day_path(day)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def empty_day(day: datetime.date) -> dict:
    return {"date": day.isoformat(), "generated_at": None, "notes": [], "drafts": [], "not_counted": [],
            "counted": []}


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    oscompat.replace(tmp, path)


def day_lock(day: datetime.date):
    """Exclusive lock for one day's draft file (app, CLI and the scheduler all go through it)."""
    return oscompat.file_lock(DRAFTS_DIR / f".{day.isoformat()}.lock")


def update_day(day: datetime.date, fn) -> dict:
    """Read-modify-write a day under its lock; fn mutates the data in place and may return a value."""
    with day_lock(day):
        data = load_day(day) or empty_day(day)
        result = fn(data)
        _write_json(day_path(day), data)
    return result if result is not None else data


def replaceable(d: dict) -> bool:
    """Drafts a regeneration may replace: untouched ones. Your edits and decisions are kept."""
    return d["status"] in ("pending", "in_zoho", "failed") and not d["edited"] and not d["manual"]


def merge_regenerated(old: dict, new: dict) -> dict:
    kept = [d for d in old["drafts"] if not replaceable(d)]
    # A job you declined or are still editing isn't offered again; a submitted job is re-checked
    # against Zoho by the new draft itself (it becomes in_zoho or shows the remainder).
    blocked = {d["job_id"] for d in kept if d["job_id"] and d["status"] in ("declined", "pending", "failed")}
    counted = old.get("counted", [])
    return {**new,
            "drafts": kept + [d for d in new["drafts"] if not (d["job_id"] and d["job_id"] in blocked)],
            "not_counted": [n for n in new["not_counted"] if n["label"] not in counted],
            "counted": counted}


def find_draft(data: dict, draft_id: str) -> dict:
    for d in data["drafts"]:
        if d["id"] == draft_id:
            return d
    raise KeyError(f"no draft {draft_id} on {data['date']}")


# ── Generate ───────────────────────────────────────────────────────────────────

class Busy(RuntimeError):
    pass


@contextlib.contextmanager
def generation_lock():
    """One generation at a time (each costs Claude calls). Stale after an hour."""
    TIMELOG_DIR.mkdir(parents=True, exist_ok=True)
    if GEN_LOCK.exists() and time.time() - GEN_LOCK.stat().st_mtime > GEN_LOCK_STALE:
        GEN_LOCK.rmdir()
    try:
        GEN_LOCK.mkdir()
    except FileExistsError:
        raise Busy("Drafts are already being generated; try again in a minute.") from None
    try:
        yield
    finally:
        GEN_LOCK.rmdir()


def generate_day(day: datetime.date) -> dict:
    """Build the day's drafts and save them, keeping anything you've already reviewed."""
    with generation_lock():
        mapping = load_mapping()
        meetings, note = outlook_meetings(day)
        notes = [note] if note else []
        timeline = build_timeline(day, load_activity(day), meetings, mapping)
        token = zoho.token()
        catalog = zoho.catalog()
        answers: dict = {"entries": {}, "buckets": {}}
        if timeline["groups"]:
            try:
                answers = ask_claude(claude_payload(day, timeline, catalog))
            except CLAUDE_ERRORS as exc:
                notes.append(f"Claude couldn't choose jobs or write descriptions ({str(exc)[:NOTE_CHARS]}). "
                             "Fill them in, or regenerate later.")
        drafts, not_counted = build_drafts(timeline, catalog, answers, zoho.fetch_existing_logs(day, token, strict=True))
        new = {**empty_day(day), "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
               "notes": notes, "drafts": drafts, "not_counted": not_counted}
        with day_lock(day):
            old = load_day(day)
            data = merge_regenerated(old, new) if old else new
            _write_json(day_path(day), data)
    write_run_log(data)
    return data


# ── Review actions ─────────────────────────────────────────────────────────────

def _form(catalog: list[dict], fields: dict) -> tuple[dict, dict, int]:
    """The project, job and minutes from the edit/add form; ValueError if any is invalid."""
    project = find_project(catalog, pid=fields.get("project_id"))
    job = find_job(project, jid=fields.get("job_id"))
    if not project or not job:
        raise ValueError("Choose a project and a job from the list")
    minutes = parse_hhmm(fields.get("hours", ""))
    if minutes <= 0:
        raise ValueError("Hours must be more than 0")
    return project, job, minutes


def edit_draft(day: datetime.date, draft_id: str, fields: dict, catalog: list[dict]) -> dict:
    project, job, minutes = _form(catalog, fields)

    def change(data):
        d = find_draft(data, draft_id)
        if d["status"] not in OPEN:
            raise ValueError(f"This entry is {d['status'].replace('_', ' ')} and can't be edited")
        d.update(project=project["name"], project_id=project["id"], job=job["name"], job_id=job["id"],
                 minutes=minutes, hours=hhmm(minutes), description=fields.get("description", "").strip(),
                 edited=True, status="pending", error=None)
        return d
    return update_day(day, change)


def add_draft(day: datetime.date, fields: dict, catalog: list[dict]) -> dict:
    """A manual entry; with not_counted_id it also removes that item from the not-counted list."""
    project, job, minutes = _form(catalog, fields)

    def change(data):
        d = new_draft(project=project["name"], project_id=project["id"], job=job["name"], job_id=job["id"],
                      minutes=minutes, description=fields.get("description", "").strip(), manual=True,
                      sources={"Added by you": minutes})
        nid = fields.get("not_counted_id")
        if nid:
            item = next((n for n in data["not_counted"] if n["id"] == nid), None)
            if item:
                data["not_counted"].remove(item)
                data.setdefault("counted", []).append(item["label"])
                d["items"] = [f"{item['kind']} {item['label']}"]
                d["sources"] = {"Counted by you": minutes}
        data["drafts"].append(d)
        return d
    return update_day(day, change)


def set_status(day: datetime.date, draft_id: str, status: str) -> dict:
    """decline, or undo a decline (back to pending)."""
    def change(data):
        d = find_draft(data, draft_id)
        if status == "declined" and d["status"] not in OPEN:
            raise ValueError(f"This entry is {d['status'].replace('_', ' ')}")
        if status == "pending" and d["status"] != "declined":
            raise ValueError("Only a declined entry can be restored")
        d["status"] = status
        return d
    return update_day(day, change)


def submit(day: datetime.date, draft_id: str, token: str | None = None) -> dict:
    """Log one draft to Zoho. Logs only what Zoho doesn't already have for that job and day."""
    with day_lock(day):
        data = load_day(day) or empty_day(day)
        d = find_draft(data, draft_id)
        if d["status"] not in OPEN:
            raise ValueError(f"This entry is {d['status'].replace('_', ' ')}")
        if not d["project_id"] or not d["job_id"]:
            raise ValueError("Pick a project and a job first (Edit)")
        if not d["description"].strip():
            raise ValueError("Add a description first (Edit)")
        token = token or zoho.token()
        logs = zoho.fetch_existing_logs(day, token, strict=True)
        d["zoho_minutes"] = zoho_minutes(logs, d["job_id"])
        remaining = d["minutes"] - d["zoho_minutes"]
        if remaining <= 0:
            d.update(status="in_zoho", error=None)
        else:
            res = zoho.add_timelog(d["job_id"], day, d["description"], token, project_id=d["project_id"],
                                   hours=hhmm(remaining), label=f"{d['project']} | {d['job']}")
            if res["success"]:
                d.update(status="submitted", error=None, logged_minutes=remaining, log_id=res["log_id"],
                         submitted_at=datetime.datetime.now().isoformat(timespec="seconds"))
            else:
                d.update(status="failed", error=res["error"][:ERROR_CHARS])
        _write_json(day_path(day), data)
    log_action(day, d)
    return d


def accept_all(day: datetime.date) -> list[dict]:
    data = load_day(day) or empty_day(day)
    token = zoho.token()
    results = []
    for d in [d for d in data["drafts"] if d["status"] in OPEN]:
        try:
            results.append(submit(day, d["id"], token))
        except ValueError as exc:
            results.append({**d, "error": str(exc)})
    return results


# ── Days: marks, missed, overview ──────────────────────────────────────────────

def load_days_file() -> dict:
    data = json.loads(DAYS_FILE.read_text(encoding="utf-8")) if DAYS_FILE.exists() else {}
    return {"since": data.get("since"), "drafts_since": data.get("drafts_since"), "marks": data.get("marks", {})}


def init_days_file(today: datetime.date) -> None:
    """Missed days are reported from 14 days before install; drafts are generated from install on."""
    data = load_days_file()
    data["since"] = data["since"] or (today - datetime.timedelta(days=MISSED_WINDOW)).isoformat()
    data["drafts_since"] = data["drafts_since"] or today.isoformat()
    _write_json(DAYS_FILE, data)


def set_mark(day: datetime.date, mark: str | None) -> dict:
    if mark is not None and mark not in MARKS:
        raise ValueError(f"mark must be one of {MARKS}")
    data = load_days_file()
    if mark:
        data["marks"][day.isoformat()] = mark
    else:
        data["marks"].pop(day.isoformat(), None)
    _write_json(DAYS_FILE, data)
    return data


def missed_days(today: datetime.date, logged: dict[str, int], marks: dict, since: str | None,
                window: int = MISSED_WINDOW) -> list[datetime.date]:
    """Weekdays in the window before today with no Zoho minutes, not marked, not before `since`."""
    out = []
    for back in range(window, 0, -1):
        d = today - datetime.timedelta(days=back)
        if d.weekday() >= 5 or (since and d.isoformat() < since) or d.isoformat() in marks:
            continue
        if not logged.get(d.isoformat()):
            out.append(d)
    return out


def zoho_minutes_by_date(today: datetime.date, token: str, window: int = MISSED_WINDOW) -> dict[str, int]:
    logs = zoho.fetch_logs_range(today - datetime.timedelta(days=window), today, token, strict=True)
    by_date: dict[str, int] = {}
    for log in logs:
        key = str(log.get("workDate", ""))[:10]
        by_date[key] = by_date.get(key, 0) + log_minutes(log)
    return by_date


def overview(today: datetime.date, token: str | None = None) -> list[dict]:
    """The last MISSED_WINDOW days plus today, newest first, for the app's day list.

    Leave and holidays come from Zoho as well as from your own marks; yours win. A row's "off" says
    where its leave/holiday came from ("zoho" or "you").
    """
    days_file = load_days_file()
    token = token or zoho.token()
    logged = zoho_minutes_by_date(today, token)
    zoho_off, _ = zoho.days_off(today - datetime.timedelta(days=MISSED_WINDOW), today, token)
    marks = {**zoho_off, **days_file["marks"]}
    missed = set(missed_days(today, logged, marks, days_file["since"]))
    rows = []
    for back in range(0, MISSED_WINDOW + 1):
        d = today - datetime.timedelta(days=back)
        open_ = sum(x["status"] in OPEN for x in (load_day(d) or {}).get("drafts", []))
        mark = marks.get(d.isoformat())
        state = ("today" if back == 0 else mark if mark else "missed" if d in missed
                 else "review" if open_ else "logged" if logged.get(d.isoformat())
                 else "weekend" if d.weekday() >= 5 else "none")
        off = "you" if d.isoformat() in days_file["marks"] else "zoho" if d.isoformat() in zoho_off else None
        rows.append({"date": d.isoformat(), "weekday": d.strftime("%a"), "state": state,
                     "zoho_minutes": logged.get(d.isoformat(), 0), "open": open_, "off": off})
    return rows


def days_to_generate(today: datetime.date) -> list[datetime.date]:
    """Finished days of the last BACKLOG_DAYS, from install on, whose drafts were never made
    after the day ended (none yet, or only a same-day preview from the app): weekdays always
    (meetings), weekends only with Claude Code activity. Regenerating keeps your decisions."""
    since = load_days_file()["drafts_since"]
    out = []
    for back in range(BACKLOG_DAYS, 0, -1):
        d = today - datetime.timedelta(days=back)
        if since and d.isoformat() < since:
            continue
        made = (load_day(d) or {}).get("generated_at")
        if made and datetime.datetime.fromisoformat(made).date() > d:
            continue
        if d.weekday() < 5 or load_activity(d):
            out.append(d)
    return out


# ── Notification and run.log ───────────────────────────────────────────────────

def notification_text(missed: list[datetime.date], to_review: int) -> str | None:
    parts = []
    if missed:
        shown = ", ".join(d.strftime("%a %d %b") for d in missed[:4]) + (" …" if len(missed) > 4 else "")
        parts.append(f"Missed: {shown}")
    if to_review:
        parts.append(f"{to_review} draft{'s' if to_review != 1 else ''} to review")
    return " · ".join(parts) or None


def notify(message: str, open_date: datetime.date | None = None) -> None:
    """Desktop notification; where the OS allows, a click opens the review app on that date."""
    url = APP_URL + (f"/#{open_date.isoformat()}" if open_date else "/")
    oscompat.notify("Zoho time log", message, url)


def write_run_log(data: dict) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"\n## {data['date']}  drafts generated {now}\n"]
    if data["drafts"]:
        lines += ["| Project | Job | Hours | Description | Status |", "|---|---|---|---|---|"]
        for d in data["drafts"]:
            status = d["status"] + (f": {d['error']}" if d.get("error") else "")
            lines.append(f"| {d['project']} | {d['job'] or '?'} | {d['hours']} | {d['description']} | {status} |"
                         .replace("\n", " "))
    else:
        lines.append("No drafts.")
    lines += [f"- not counted: {n['label']} ({hhmm(int(n['minutes']))}, {n['reason']})" for n in data["not_counted"]]
    lines += [f"- note: {n}" for n in data["notes"]]
    _append_log("\n".join(lines))


def log_action(day: datetime.date, d: dict) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    extra = f" ({hhmm(d['logged_minutes'])} logged)" if d.get("logged_minutes") else ""
    _append_log(f"- {now} {d['status']}{extra}: {day} {d['project']} / {d['job']} {d['hours']} — "
                f"{d['description']}" + (f" [{d['error']}]" if d.get("error") else ""))


def _append_log(text: str) -> None:
    TIMELOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)
