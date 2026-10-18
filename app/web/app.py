"""FastAPI web UI + ``/healthz`` on the same port as the old health server."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import Settings
from app.logging_setup import get_logger
from app.models import REJECT_REASONS, STATUS_ORDER, Status
from app.storage import Database

log = get_logger(__name__)


def _optional_float(value: Any) -> float | None:
    """HTML forms send ``min_score=`` as an empty string — treat as unset."""
    if value is None or value == "":
        return None
    return float(value)


# Query() must live inside Annotated so BeforeValidator runs before float parsing.
# Default goes on `=` (FastAPI rejects default= inside Annotated Query).
OptionalFloatQuery = Annotated[
    float | None, BeforeValidator(_optional_float), Query()
]

WEB_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(WEB_ROOT / "templates"))
STATUS_LABELS = {
    Status.NEW.value: "новые",
    Status.SAVED.value: "сохранённые",
    Status.APPLIED.value: "отклик",
    Status.SCREENING.value: "скрининг",
    Status.TECH.value: "тех",
    Status.OFFER.value: "оффер",
    Status.REJECTED.value: "отказ",
    Status.IGNORED.value: "не интересно",
    Status.ARCHIVED.value: "архив",
}

#: The default feed hides the archive and anything the user already dismissed.
#: ``✕`` sets ``ignored``, so without this the row stayed put after the reload and
#: the button looked like it did nothing. Both stay reachable via ``?status=…``.
FEED_EXCLUDED = [Status.IGNORED.value, Status.ARCHIVED.value]


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("sources", "score_breakdown", "flags", "raw"):
        raw = data.get(key)
        if isinstance(raw, str):
            with contextlib.suppress(json.JSONDecodeError):
                data[key] = json.loads(raw)
    return data


def _salary_text(row: dict[str, Any]) -> str:
    lo, hi = row.get("salary_min"), row.get("salary_max")
    cur = row.get("currency") or ""
    if lo is None and hi is None:
        return "не указана"
    suffix = f" {cur}" if cur else ""
    if lo is not None and hi is not None:
        return f"{int(lo)}–{int(hi)}{suffix}"
    only = lo if lo is not None else hi
    prefix = "от" if lo is not None else "до"
    return f"{prefix} {int(only)}{suffix}"


def _channel_of(row: dict[str, Any]) -> str:
    raw = row.get("raw") or {}
    if isinstance(raw, dict):
        return str(raw.get("channel") or "")
    return ""


def _here(request: Request) -> str:
    """Relative URL of the current page — the value a form posts as ``next``."""
    parts = urlsplit(str(request.url))
    return f"{parts.path}?{parts.query}" if parts.query else (parts.path or "/")


def _redirect_target(value: str, request: Request, job_id: str) -> str:
    """Turn the ``next`` a form posted into a same-site relative URL.

    The feed posts ``request.url`` — an *absolute* URL, port included. The old
    check (``startswith("/")``) rejected it, so ★/✓/✕ always bounced to the
    vacancy card and the list looked like it did nothing.
    """
    target = (value or "").strip()
    if not target:
        return f"/jobs/{job_id}"
    if target.startswith("/"):
        return target
    parts = urlsplit(target)
    # Off-site target: stay inside the app instead of following it blindly.
    # The hostname is the comparison, not the netloc — the browser may spell the
    # same origin with an explicit port (``host`` vs ``host:80``).
    if parts.netloc and parts.hostname != request.url.hostname:
        return f"/jobs/{job_id}"
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Optional shared-secret gate. Empty token = open (local/dev)."""

    def __init__(self, app: Any, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if not self.token:
            return await call_next(request)
        path = request.url.path
        if path in ("/healthz", "/health"):
            return await call_next(request)

        cookie = request.cookies.get("jh_token", "")
        header = request.headers.get("authorization", "")
        bearer = (
            header.removeprefix("Bearer ").strip()
            if header.lower().startswith("bearer ")
            else ""
        )
        query = request.query_params.get("token", "")
        provided = cookie or bearer or query

        if provided != self.token:
            if path.startswith("/api/"):
                return Response(
                    '{"detail":"unauthorized"}',
                    status_code=401,
                    media_type="application/json",
                )
            return HTMLResponse(_login_page(), status_code=401)

        response = await call_next(request)
        if query == self.token:
            # Persist token, then drop it from the address bar.
            params = {k: v for k, v in request.query_params.items() if k != "token"}
            clean = request.url.path
            if params:
                clean = f"{clean}?{urlencode(params)}"
            redirect = RedirectResponse(clean, status_code=303)
            redirect.set_cookie(
                "jh_token",
                self.token,
                httponly=True,
                samesite="lax",
                max_age=60 * 60 * 24 * 30,
            )
            return redirect
        return response


def _login_page() -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>jobhunter</title>"
        "<body style='font-family:system-ui;background:#081018;color:#e8eef7;"
        "display:grid;place-items:center;min-height:100vh;margin:0'>"
        "<form method=get style='display:flex;gap:.5rem'>"
        "<input name=token type=password placeholder='WEB_TOKEN' "
        "style='padding:.65rem .85rem;border-radius:10px;border:1px solid #243247;"
        "background:#121c2a;color:inherit'>"
        "<button style='padding:.65rem 1rem;border:0;border-radius:10px;"
        "background:#d6ff3f;color:#0b1408;font-weight:700'>войти</button>"
        "</form></body>"
    )


def create_web_app(
    *,
    db: Database,
    settings: Settings,
    pipeline: Any | None = None,
    healthy: Callable[[], bool] | None = None,
) -> FastAPI:
    app = FastAPI(title="jobhunter", docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.settings = settings
    app.state.pipeline = pipeline
    app.state._healthy = True

    if settings.web_token:
        app.add_middleware(TokenAuthMiddleware, token=settings.web_token)

    static_dir = WEB_ROOT / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def require_db() -> Database:
        return app.state.db

    @app.get("/healthz")
    @app.get("/health")
    async def healthz() -> Response:
        ok = app.state._healthy if healthy is None else bool(healthy())
        return Response(
            "ok" if ok else "degraded",
            status_code=200 if ok else 503,
            media_type="text/plain",
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        status: str | None = None,
        q: str | None = None,
        source: str | None = None,
        channel: str | None = None,
        min_score: OptionalFloatQuery = None,
        page: int = Query(default=1, ge=1),
        db_: Database = Depends(require_db),
    ) -> HTMLResponse:
        limit = 40
        offset = (page - 1) * limit
        statuses = [status] if status else None
        excluded = None if statuses else FEED_EXCLUDED
        rows = await db_.list_jobs(
            statuses=statuses,
            exclude_statuses=excluded,
            min_score=min_score,
            q=q,
            source=source,
            channel=channel,
            limit=limit,
            offset=offset,
        )
        total = await db_.count_jobs(
            statuses=statuses,
            exclude_statuses=excluded,
            min_score=min_score,
            q=q,
            source=source,
            channel=channel,
        )
        funnel = await db_.funnel_counts()
        jobs = []
        for row in rows:
            item = _row_to_dict(row)
            item["salary_text"] = _salary_text(item)
            item["channel"] = _channel_of(item)
            jobs.append(item)
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "jobs": jobs,
                "funnel": funnel,
                "statuses": list(STATUS_ORDER),
                "status_labels": STATUS_LABELS,
                "filters": {
                    "status": status or "",
                    "q": q or "",
                    "source": source or "",
                    "channel": channel or "",
                    "min_score": "" if min_score is None else min_score,
                    "page": page,
                },
                "total": total,
                "pages": max(1, (total + limit - 1) // limit),
                "return_here": _here(request),
                "reject_reasons": REJECT_REASONS,
            },
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_page(
        request: Request, job_id: str, db_: Database = Depends(require_db)
    ) -> HTMLResponse:
        row = await db_.get_job(job_id)
        if row is None:
            raise HTTPException(404, "job not found")
        job = _row_to_dict(row)
        job["salary_text"] = _salary_text(job)
        job["channel"] = _channel_of(job)
        return TEMPLATES.TemplateResponse(
            request,
            "job.html",
            {
                "job": job,
                "statuses": list(STATUS_ORDER),
                "status_labels": STATUS_LABELS,
                "reject_reasons": REJECT_REASONS,
            },
        )

    @app.post("/jobs/{job_id}/status")
    async def set_status(
        job_id: str,
        request: Request,
        status: str = Form(...),
        reason: str = Form(""),
        next: str = Form("/"),
        db_: Database = Depends(require_db),
    ) -> RedirectResponse:
        if status not in {s.value for s in STATUS_ORDER}:
            raise HTTPException(400, "bad status")
        ok = await db_.set_status(job_id, status, reason=reason)
        if not ok:
            raise HTTPException(404, "job not found")
        await db_.log_event(job_id, "web_status", f"{status}:{reason}")
        return RedirectResponse(
            _redirect_target(next, request, job_id),
            status_code=303,
        )

    @app.get("/api/jobs")
    async def api_jobs(
        status: str | None = None,
        q: str | None = None,
        source: str | None = None,
        channel: str | None = None,
        min_score: OptionalFloatQuery = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        db_: Database = Depends(require_db),
    ) -> dict[str, Any]:
        statuses = [status] if status else None
        excluded = None if statuses else FEED_EXCLUDED
        rows = await db_.list_jobs(
            statuses=statuses,
            exclude_statuses=excluded,
            min_score=min_score,
            q=q,
            source=source,
            channel=channel,
            limit=limit,
            offset=offset,
        )
        total = await db_.count_jobs(
            statuses=statuses,
            exclude_statuses=excluded,
            min_score=min_score,
            q=q,
            source=source,
            channel=channel,
        )
        return {"total": total, "items": [_row_to_dict(r) for r in rows]}

    @app.get("/api/stats")
    async def api_stats(db_: Database = Depends(require_db)) -> dict[str, Any]:
        return {
            "funnel": await db_.funnel_counts(),
            "sources": [
                {"name": n, "count": c, "avg_score": round(a, 1)}
                for n, c, a in await db_.top_sources()
            ],
            "health": [
                {
                    "name": h.name,
                    "ok": h.ok,
                    "jobs_found": h.jobs_found,
                    "consecutive_errors": h.consecutive_errors,
                    "last_error": h.last_error,
                }
                for h in await db_.source_health_all()
            ],
        }

    @app.post("/api/run")
    async def api_run() -> dict[str, Any]:
        pipe = app.state.pipeline
        if pipe is None:
            raise HTTPException(503, "pipeline unavailable")
        summary = await pipe.run(dry_run=False)
        return {"ok": summary.ok_sources, "text": summary.as_text(), "new": len(summary.new_ids)}

    return app


async def serve_web(app: FastAPI, *, host: str, port: int) -> None:
    """Run uvicorn until cancelled (used from the main event loop)."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    # Don't install its own signal handlers — ``app.main`` owns SIGINT/SIGTERM.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    log.info("web UI listening on http://%s:%d/", host, port)
    await server.serve()


__all__ = ["create_web_app", "serve_web"]
