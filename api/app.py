"""FastAPI application: JSON API + server-rendered dashboard shell. Hub only.

Bind to the Tailscale interface (hub.bind). HTTPS is terminated by `tailscale serve` (or a
Cloudflare Tunnel); the app itself speaks plain HTTP on the private interface only.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent import __version__
from agent.config import Config
from agent.db import Db

from . import queries as q
from .auth import COOKIE, Auth

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "dashboard" / "templates"
STATIC = ROOT / "dashboard" / "static"


def create_app(cfg: Config, db: Db | None = None) -> FastAPI:
    db = db or Db(cfg.db_path)
    auth = Auth(cfg.data_dir, session_days=int(cfg.get("hub.session_days", 30)),
                lockout_s=int(cfg.get("hub.login_lockout_s", 60)), secure=bool(cfg.get("hub.secure_cookies", False)))
    app = FastAPI(title="rig-overseer", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg, app.state.db, app.state.auth = cfg, db, auth
    templates = Jinja2Templates(directory=str(TEMPLATES))
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    # --- auth dependencies ---------------------------------------------------------------------
    def ui_user(request: Request):
        if auth.verify(request.cookies.get(COOKIE)):
            return "session"
        raise HTTPException(status_code=303, headers={"Location": "/login?next=" + request.url.path})

    def api_user(request: Request):
        if auth.verify(request.cookies.get(COOKIE)) or auth.check_bearer(request.headers.get("authorization")):
            return "ok"
        raise HTTPException(status_code=401, detail="auth required")

    def worker_user(request: Request):
        if auth.check_bearer(request.headers.get("authorization")):
            return "worker"
        raise HTTPException(status_code=401, detail="bearer token required")

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"cfg_host": cfg.host_name, "version": __version__, **ctx})

    # --- public ---------------------------------------------------------------------------------
    @app.get("/health")
    def health():
        last = db.one("SELECT MAX(ts) ts FROM host_samples")
        return {"ok": True, "host": cfg.host_name, "last_sample_age_s": (time.time() - last["ts"]) if last and last["ts"] else None}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return page(request, "login.html", next=next, configured=auth.password_configured(), error=None)

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        client = request.client.host if request.client else "?"
        if not auth.check_password(client, str(form.get("password", ""))):
            log.warning("failed dashboard login from %s", client)
            return page(request, "login.html", next=form.get("next", "/"), configured=auth.password_configured(),
                        error="Wrong password or too many attempts; wait a minute.")
        nxt = str(form.get("next") or "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(COOKIE, auth.issue(), max_age=auth.ttl, httponly=True, samesite="lax", secure=auth.secure, path="/")
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # --- pages ----------------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def p_overview(request: Request, _=Depends(ui_user)):
        return page(request, "overview.html", nav="overview")

    @app.get("/gpus", response_class=HTMLResponse)
    def p_gpus(request: Request, _=Depends(ui_user)):
        return page(request, "gpus.html", nav="gpus")

    @app.get("/gpu/{gpu_id}", response_class=HTMLResponse)
    def p_gpu(request: Request, gpu_id: int, _=Depends(ui_user)):
        g = db.one("SELECT g.*, h.name host FROM gpus g JOIN hosts h ON h.id=g.host_id WHERE g.id=?", (gpu_id,))
        if not g:
            raise HTTPException(404)
        return page(request, "gpu.html", nav="gpus", gpu=dict(g))

    @app.get("/log", response_class=HTMLResponse)
    def p_log(request: Request, _=Depends(ui_user)):
        return page(request, "log.html", nav="log")

    @app.get("/claude", response_class=HTMLResponse)
    def p_claude(request: Request, _=Depends(ui_user)):
        return page(request, "claude.html", nav="claude")

    @app.get("/incidents", response_class=HTMLResponse)
    def p_incidents(request: Request, _=Depends(ui_user)):
        return page(request, "incidents.html", nav="incidents")

    # --- JSON API -------------------------------------------------------------------------------
    @app.get("/api/overview")
    def a_overview(_=Depends(api_user)):
        return q.overview(db)

    @app.get("/api/gpus")
    def a_gpus(_=Depends(api_user)):
        return q.gpus_latest(db)

    @app.get("/api/gpus/{gpu_id}/series")
    def a_series(gpu_id: int, hours: float = 24, res: str = "5m", _=Depends(api_user)):
        if res not in ("5m", "15s"):
            raise HTTPException(400, "res must be 5m or 15s")
        hours = max(0.25, min(hours, 24 * 400))
        return q.series(db, gpu_id, hours, res)

    @app.get("/api/gpus/{gpu_id}/detail")
    def a_detail(gpu_id: int, _=Depends(api_user)):
        g = next((x for x in q.gpus_latest(db) if x["id"] == gpu_id), None)
        if not g:
            raise HTTPException(404)
        return {"gpu": g, "trials": q.trials(db, gpu_id), "actions": q.actions(db, 30, gpu_id),
                "profiles": {st: db.profile(gpu_id, st) for st in ("active", "known_good", "converged")}}

    @app.get("/api/log")
    def a_log(host: str | None = None, gpu: int | None = None, since: float | None = None, until: float | None = None,
              res: str = "5m", limit: int = 200, offset: int = 0, _=Depends(api_user)):
        return q.log_rows(db, host=host, gpu_id=gpu, since=since, until=until, res=res, limit=min(limit, 5000), offset=offset)

    @app.get("/api/log.csv")
    def a_log_csv(host: str | None = None, gpu: int | None = None, since: float | None = None, until: float | None = None,
                  res: str = "5m", limit: int = 50000, _=Depends(api_user)):
        rows = q.log_rows(db, host=host, gpu_id=gpu, since=since, until=until, res=res, limit=min(limit, 200000), offset=0)
        fname = f"rig-log-{time.strftime('%Y%m%d-%H%M')}.csv"
        return PlainTextResponse(q.log_csv(rows), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.get("/api/claude-runs")
    def a_runs(limit: int = 50, _=Depends(api_user)):
        return q.claude_runs(db, min(limit, 500))

    @app.get("/api/claude-runs/{run_id}")
    def a_run(run_id: str, _=Depends(api_user)):
        r = q.claude_run(db, run_id)
        if not r:
            raise HTTPException(404)
        return r

    @app.get("/api/incidents")
    def a_incidents(limit: int = 100, _=Depends(api_user)):
        return q.incidents(db, min(limit, 1000))

    @app.get("/api/actions")
    def a_actions(limit: int = 100, gpu: int | None = None, _=Depends(api_user)):
        return q.actions(db, min(limit, 1000), gpu)

    # --- worker ingest --------------------------------------------------------------------------
    @app.post("/api/ingest")
    async def ingest(request: Request, _=Depends(worker_user)):
        body = await request.json()
        host = str(body.get("host") or "")
        if not host:
            raise HTTPException(400, "host required")
        n = 0
        for item in body.get("items", []):
            try:
                _ingest_one(db, host, item.get("kind"), item.get("payload") or {})
                n += 1
            except Exception as e:                           # one bad row must not block the batch
                log.warning("ingest %s from %s failed: %s", item.get("kind"), host, str(e)[:120])
        return {"ok": True, "accepted": n}

    @app.exception_handler(HTTPException)
    async def _redirects(request: Request, exc: HTTPException):
        if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    return app


def _ingest_one(db: Db, host: str, kind: str, p: dict) -> None:
    host_id = db.upsert_host(host, p.get("os") if kind == "host_info" else None, "worker",
                             p.get("meta") if kind == "host_info" else None)
    if kind == "host_info":
        return
    if kind == "gpu_info":
        db.upsert_gpu(host_id, p["uuid"], p.get("idx", 0), p.get("model", "unknown"), p.get("bus_id"), p.get("stock_pl_w"),
                      p.get("min_pl_w"), p.get("max_pl_w"), p.get("vram_mb"), bool(p.get("control_enabled")))
    elif kind == "gpu_sample":
        gid = db.gpu_id(p["uuid"]) or db.upsert_gpu(host_id, p["uuid"], 0, "unknown", None, None, None, None, None, False)
        cols = {k: v for k, v in p.items() if k not in ("uuid", "ts", "res")}
        db.insert_gpu_sample(gid, float(p["ts"]), p.get("res", "5m"), **cols)
    elif kind == "host_sample":
        cols = {k: v for k, v in p.items() if k not in ("host", "ts", "res")}
        db.insert_host_sample(host_id, float(p["ts"]), p.get("res", "5m"), **cols)
    elif kind == "action":
        gid = db.gpu_id(p["gpu_uuid"]) if p.get("gpu_uuid") else None
        db.log_action(p["action"], p.get("actor", "rule"), bool(p.get("ok")), bool(p.get("dry_run")), host_id=host_id,
                      gpu_id=gid, params=p.get("params"), before=p.get("before"), after=p.get("after"),
                      reason=p.get("reason"), run_id=p.get("run_id"), error=p.get("error"), ts=p.get("ts"))
    elif kind == "incident":
        gid = db.gpu_id(p["gpu_uuid"]) if p.get("gpu_uuid") else None
        key = f"ingest.incident.{host}.{p.get('local_id')}"
        hub_id = db.kv_get(key)
        if hub_id:
            db.update_incident(hub_id, status=p.get("status", "open"), ladder_step=p.get("ladder_step"),
                               summary=p.get("summary"), closed=p.get("closed"))
        else:
            hub_id = db.open_incident(p["rule"], p.get("severity", "high"), host_id=host_id, gpu_id=gid,
                                      ladder_step=p.get("ladder_step"), summary=p.get("summary"), opened=p.get("opened"))
            db.kv_set(key, hub_id)
    elif kind == "state":
        db.kv_set(f"{p['key']}.{host}", p.get("value"))
    else:
        raise ValueError(f"unknown kind {kind!r}")


def serve(cfg: Config) -> None:
    import uvicorn
    from agent import secrets
    from agent.log import setup_logging

    secrets.configure(cfg.secrets_file)
    setup_logging(cfg.data_dir, "web", cfg.get("logging.level", "INFO"))
    if not cfg.is_hub:
        raise SystemExit("the web server runs on the hub only (host.role: hub)")
    host, port = str(cfg.get("hub.bind", "127.0.0.1")), int(cfg.get("hub.port", 8080))
    if host == "0.0.0.0":
        log.warning("hub.bind is 0.0.0.0: make sure the firewall limits this to the Tailscale interface")
    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False, workers=1)
