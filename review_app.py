"""Review app: accept, edit or decline draft time logs before they reach Zoho.

Runs on 127.0.0.1 only (`python timelog.py app`, or the launchd agent). Requests must carry an
allowed Host header (blocks DNS rebinding), and every write needs `X-Timelog: 1`, which a
cross-site form can't send without a CORS preflight that this app never allows.
"""

import datetime

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

import drafts as dr
import zoho_api as zoho
from constants import ERROR_CHARS, HOST, INDEX_HTML, PORT

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def _day(request: Request) -> datetime.date:
    try:
        return datetime.date.fromisoformat(request.path_params["date"])
    except ValueError:
        raise ValueError("Bad date") from None


def api(fn):
    """Run a blocking handler in the threadpool and turn errors into JSON with a status code."""
    async def endpoint(request: Request):
        body = {}
        if request.method in ("POST", "PUT"):
            try:
                body = await request.json()
            except ValueError:
                body = {}
        try:
            return JSONResponse(await run_in_threadpool(fn, request, body))
        except (ValueError, KeyError) as exc:
            return JSONResponse({"error": str(exc).strip("'\"")}, status_code=400)
        except dr.Busy as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:  # Zoho / Claude / network
            return JSONResponse({"error": f"{type(exc).__name__}: {str(exc)[:ERROR_CHARS]}"}, status_code=502)
    return endpoint


# ── Handlers (blocking) ────────────────────────────────────────────────────────

def days(request, body):
    return {"days": dr.overview(datetime.date.today())}


def day_detail(request, body):
    day = _day(request)
    data = dr.load_day(day)
    in_zoho, zoho_error = [], None
    try:
        full = zoho.catalog()
        names = {p["id"]: p["name"] for p in full}
        jobs = {j["id"]: j for p in full for j in p["jobs"]}
        for d in (data or {}).get("drafts", []):
            d["job_closed"] = bool(d.get("job_id") and d["job_id"] in jobs and not zoho.job_open(jobs[d["job_id"]], day))
        for log in zoho.fetch_existing_logs(day, zoho.token(), strict=True):
            in_zoho.append({"project": log.get("projectName") or names.get(str(log.get("projectId")), "?"),
                            "job": log.get("jobName", "?"), "hours": dr.hhmm(zoho.log_minutes(log)),
                            "description": log.get("taskName") or log.get("description") or ""})
    except Exception as exc:
        zoho_error = str(exc)[:ERROR_CHARS]
    return {"date": day.isoformat(), "exists": data is not None,
            "is_today": day == datetime.date.today(), "is_future": day > datetime.date.today(),
            "mark": dr.load_days_file()["marks"].get(day.isoformat()),
            **(data or dr.empty_day(day)), "zoho": in_zoho, "zoho_error": zoho_error}


def catalog(request, body):
    """?date=YYYY-MM-DD → only jobs open that day; add all=1 for everything (the form's "Show all jobs")."""
    projects = zoho.catalog(fresh=request.query_params.get("fresh") == "1")
    date = request.query_params.get("date")
    if date and request.query_params.get("all") != "1":
        projects = zoho.open_catalog(projects, datetime.date.fromisoformat(date))
    return {"projects": projects}


def draft_action(request, body):
    day, draft_id, action = _day(request), request.path_params["id"], request.path_params["action"]
    if action == "accept":
        return dr.submit(day, draft_id)
    if action == "decline":
        return dr.set_status(day, draft_id, "declined")
    if action == "undo":
        return dr.set_status(day, draft_id, "pending")
    raise ValueError(f"Unknown action {action}")


def draft_edit(request, body):
    return dr.edit_draft(_day(request), request.path_params["id"], body, zoho.catalog())


def draft_add(request, body):
    return dr.add_draft(_day(request), body, zoho.catalog())


def accept_all(request, body):
    return {"results": dr.accept_all(_day(request))}


def regenerate(request, body):
    day = _day(request)
    if day > datetime.date.today():
        raise ValueError("That day hasn't happened yet")
    return dr.generate_day(day)


def mark(request, body):
    dr.set_mark(_day(request), body.get("mark") or None)
    return {"ok": True}


# ── App ────────────────────────────────────────────────────────────────────────

def make_app(port: int = PORT) -> Starlette:
    allowed_hosts = {f"{HOST}:{port}", f"localhost:{port}"}

    async def guard(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return Response("Forbidden host", status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-timelog") != "1":
            return Response("Missing X-Timelog header", status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "X-Frame-Options": "DENY", "Content-Security-Policy": CSP,
                                 "Referrer-Policy": "no-referrer"})
        return response

    async def index(request: Request):
        return FileResponse(INDEX_HTML, media_type="text/html")

    d = "/api/day/{date}"
    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/days", api(days)),
            Route("/api/catalog", api(catalog)),
            Route(d, api(day_detail)),
            Route(d + "/draft/{id}/{action}", api(draft_action), methods=["POST"]),
            Route(d + "/draft/{id}", api(draft_edit), methods=["PUT"]),
            Route(d + "/drafts", api(draft_add), methods=["POST"]),
            Route(d + "/accept-all", api(accept_all), methods=["POST"]),
            Route(d + "/regenerate", api(regenerate), methods=["POST"]),
            Route(d + "/mark", api(mark), methods=["POST"]),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=guard)],
    )
