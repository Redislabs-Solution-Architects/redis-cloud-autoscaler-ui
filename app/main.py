"""FastAPI entry point.

Runs the bootstrap (validate config + register scaling rules) at startup,
then exposes REST + WebSocket endpoints used by the UI.

Optional HTTP Basic Auth is applied to every route (including the WebSocket
handshake) when UI_AUTH_PASSWORD is set. Empty password = open access.
"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import admin, bootstrap, config, memtier
from .state import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
logger = logging.getLogger("api")

state_mgr = StateManager()
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) Boot-time setup (idempotent, fail-soft)
    try:
        await bootstrap.run()
    except Exception as e:
        logger.exception("bootstrap error (continuing anyway): %s", e)
    # 2) Background fetcher
    bg = asyncio.create_task(state_mgr.run_forever())
    logger.info("UI ready")
    try:
        yield
    finally:
        bg.cancel()
        try:
            await bg
        except asyncio.CancelledError:
            pass
        memtier.stop()


# --------------------------------------------------------------------- auth (optional)
_AUTH_ENABLED = bool(config.UI_AUTH_PASSWORD)
if _AUTH_ENABLED:
    logger.info("HTTP Basic Auth enabled (user=%s)", config.UI_AUTH_USERNAME)

# Paths that bypass auth entirely — healthcheck + favicon need to be open
# so docker compose healthcheck and the browser tab icon work without creds.
_PUBLIC_PATHS = {"/healthz", "/favicon.ico"}


def _basic_auth_ok(authorization: str) -> bool:
    """Validate an `Authorization: Basic ...` header value."""
    if not _AUTH_ENABLED:
        return True
    if not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8", "ignore")
        user, _, pw = decoded.partition(":")
        return (secrets.compare_digest(user, config.UI_AUTH_USERNAME) and
                secrets.compare_digest(pw, config.UI_AUTH_PASSWORD))
    except Exception:
        return False


app = FastAPI(title="Redis Cloud Autoscaler UI", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    """HTTP Basic Auth on every request, except the public paths above.
    WebSocket routes don't go through HTTP middleware — they have their own
    check inside the ws endpoint."""
    if not _AUTH_ENABLED or request.url.path in _PUBLIC_PATHS:
        return await call_next(request)
    if _basic_auth_ok(request.headers.get("authorization", "")):
        return await call_next(request)
    return PlainTextResponse(
        "Unauthorized", status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="autoscaler-ui"'},
    )


# --------------------------------------------------------------------- REST
@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Unauthenticated liveness probe — used by docker compose healthcheck.
    Returns minimal info; the auth-protected /api/health is the same thing
    for callers who already have credentials."""
    return {"status": "ok"}


@app.get("/api/state")
async def get_state() -> JSONResponse:
    return JSONResponse(state_mgr.snapshot())


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return {
        "client_name":  config.CLIENT_NAME,
        "tagline":      config.CLIENT_TAGLINE,
        "db":           {"id": config.DB_ID,
                         "baseline_ops": config.BASELINE_OPS,
                         "burst_ops": config.BURST_OPS,
                         "baseline_mem_gb": config.BASELINE_MEM_GB,
                         "memory_ceiling_gb": config.MEMORY_CEILING_GB},
        "features":     {"memory_scaling_enabled": config.MEMORY_SCALING_ENABLED,
                         "memory_step_gb": config.MEMORY_STEP_GB,
                         "memory_ceiling_gb": config.MEMORY_CEILING_GB},
        "thresholds":   {"throughput_pct": config.THROUGHPUT_THRESHOLD_PCT,
                         "memory_pct":     config.MEMORY_THRESHOLD_PCT,
                         "throughput_for": config.THROUGHPUT_THRESHOLD_FOR,
                         "memory_for":     config.MEMORY_THRESHOLD_FOR},
        "presets":      config.PRESETS,
    }


# Memtier control
@app.post("/api/load/start")
async def start_load(params: dict[str, Any]) -> dict[str, Any]:
    res = await asyncio.get_event_loop().run_in_executor(None, memtier.start, params)
    return res


@app.post("/api/load/stop")
async def stop_load() -> dict[str, Any]:
    return await asyncio.get_event_loop().run_in_executor(None, memtier.stop)


@app.get("/api/load/status")
async def load_status() -> dict[str, Any]:
    return await asyncio.get_event_loop().run_in_executor(None, memtier.status)


# Admin
@app.post("/api/admin/flushdb")
async def admin_flushdb() -> dict[str, Any]:
    return await asyncio.get_event_loop().run_in_executor(None, admin.flushdb)


@app.post("/api/admin/reset-baseline")
async def admin_reset_baseline() -> dict[str, Any]:
    return await asyncio.get_event_loop().run_in_executor(None, admin.reset_to_baseline)


@app.post("/api/admin/reload-rules")
async def admin_reload_rules() -> dict[str, Any]:
    return await asyncio.get_event_loop().run_in_executor(None, admin.reload_scaling_rules)


# Auto-reset
@app.post("/api/auto-reset/cancel")
async def auto_reset_cancel() -> dict[str, Any]:
    return await state_mgr.cancel_auto_reset()


@app.post("/api/auto-reset/now")
async def auto_reset_now() -> dict[str, Any]:
    return await state_mgr.trigger_auto_reset_now()


# --------------------------------------------------------------------- WebSocket
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    if not _basic_auth_ok(websocket.headers.get("authorization", "")):
        await websocket.close(code=1008)  # policy violation
        return
    await websocket.accept()
    try:
        await websocket.send_text(json.dumps(state_mgr.snapshot()))
        while True:
            await asyncio.sleep(1.0)
            await websocket.send_text(json.dumps(state_mgr.snapshot()))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("WS error: %s", e)


# --------------------------------------------------------------------- static (must be LAST)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
