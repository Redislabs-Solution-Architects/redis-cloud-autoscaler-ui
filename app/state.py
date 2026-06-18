"""Live state aggregator.

Pulls DB shape from the Redis Cloud REST API, live metrics + alert states
from Prometheus, and parses the autoscaler container logs for events.
Also owns the auto-reset scheduler (the only path through which the DB
is brought back to baseline — there is no reactive scale-down).
"""
from __future__ import annotations
import asyncio
import http.client
import json
import logging
import socket
import subprocess
import time
from collections import deque
from threading import Lock
from typing import Any

from . import admin, config, memtier

logger = logging.getLogger("state")

# 5 minutes of history at 1 Hz
HISTORY_SECONDS = 300

# Docker daemon Unix socket — mounted read-only into the UI container.
_DOCKER_SOCK = "/var/run/docker.sock"


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """http.client connection that speaks HTTP over a Unix socket.

    The Docker daemon's REST API is served on /var/run/docker.sock and
    accepts the same HTTP semantics as a TCP daemon. Using stdlib means
    no docker CLI binary inside the image and no API-version skew with
    the host daemon.
    """
    def __init__(self, path: str, timeout: float = 4):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = path

    def connect(self) -> None:  # type: ignore[override]
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._sock_path)


def _demux_docker_stream(data: bytes) -> str:
    """Strip the 8-byte stream header Docker prepends to each log chunk
    when the container has no TTY. Header is:
        [stream_type (1B)] [padding (3B)] [size (4B big-endian)]
    followed by `size` bytes of payload.
    If parsing fails (TTY mode or unexpected format) we fall back to
    decoding the raw bytes — the regex matchers downstream will still
    find what they need.
    """
    out: list[bytes] = []
    i = 0
    n = len(data)
    try:
        while i + 8 <= n:
            size = int.from_bytes(data[i + 4:i + 8], "big")
            i += 8
            out.append(data[i:i + size])
            i += size
        # Some leftover (truncated frame at end) — append as-is.
        if i < n:
            out.append(data[i:])
        return b"".join(out).decode("utf-8", errors="replace")
    except Exception:
        return data.decode("utf-8", errors="replace")


def _docker_logs_via_socket(container: str, *, tail: int = 200,
                            timeout: float = 4) -> str | None:
    """Return last `tail` lines of stdout+stderr from `container`, or None
    if the socket is unreachable. Raises nothing — all errors swallow into
    None so the caller can decide how to surface them."""
    try:
        conn = _UnixSocketHTTPConnection(_DOCKER_SOCK, timeout=timeout)
        path = (f"/containers/{container}/logs"
                f"?stdout=1&stderr=1&tail={tail}&timestamps=0")
        conn.request("GET", path, headers={"Host": "docker"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status != 200:
            return None
        return _demux_docker_stream(body)
    except (FileNotFoundError, ConnectionRefusedError, OSError, socket.timeout):
        return None
    except Exception:
        return None


class StateManager:
    def __init__(self) -> None:
        self._lock = Lock()
        # Configured (Redis Cloud REST API)
        self.db_status: str = "loading"
        self.db_name: str = ""
        self.db_throughput: int = 0
        self.db_memlim_gb: float = 0.0
        self.db_modified: str = ""
        self.db_shards: int = 0
        # HA flag taken directly from the REST API — NOT inferred from the
        # memlim/baseline ratio (that heuristic breaks the moment we reset
        # to baseline, since memlim then equals BASELINE_MEM_GB).
        self.db_replication: bool = False
        # Live (Prometheus)
        self.live_ops: float = 0.0
        self.live_mem_bytes: float = 0.0
        # Alerts
        self.alerts: list[dict[str, str]] = []
        # Autoscaler events (parsed from container logs)
        self.events: deque[dict[str, str]] = deque(maxlen=50)
        # Throughput chart history
        self.history: deque[dict[str, float]] = deque(maxlen=HISTORY_SECONDS)
        # Memtier
        self.memtier_running: bool = False
        self.memtier_status: str = ""
        self.memtier_params: dict[str, Any] | None = None
        # Diagnostics
        self.db_fetch_err: str = ""
        self.prom_fetch_err: str = ""
        # Auto-reset
        self._auto_reset_task: asyncio.Task | None = None
        self.auto_reset_at: float | None = None
        self.auto_reset_seconds: int = config.AUTO_RESET_SECONDS
        self.auto_reset_last_action: str = ""

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "db": {
                    "status":           self.db_status,
                    "throughput":       self.db_throughput,
                    "memory_limit_gb":  self.db_memlim_gb,        # raw value from API (with HA already × 2)
                    "dataset_size_gb":  self._dataset_size_gb(),  # user-facing (memlim/2 if HA)
                    "last_modified":    self.db_modified,
                    "shards":           self.db_shards,
                    "name":             self.db_name or "managed-db",
                    "id":               config.DB_ID,
                    "baseline_ops":     config.BASELINE_OPS,
                    "burst_ops":        config.BURST_OPS,
                    "baseline_mem_gb":  config.BASELINE_MEM_GB,
                    "burst_mem_gb":     config.MEMORY_CEILING_GB,
                    "replication":      self.db_replication,
                },
                "live": {
                    "ops_per_sec":   self.live_ops,
                    "memory_bytes":  self.live_mem_bytes,
                },
                "alerts":   list(self.alerts),
                "events":   list(self.events),
                "history":  list(self.history),
                "memtier": {
                    "running":  self.memtier_running,
                    "status":   self.memtier_status,
                    "params":   self.memtier_params,
                },
                "diagnostics": {
                    "db_fetch_err":   self.db_fetch_err,
                    "prom_fetch_err": self.prom_fetch_err,
                },
                "auto_reset": {
                    "enabled":           config.AUTO_RESET_ACTIVE,
                    "scheduled":         self._auto_reset_task is not None,
                    "reset_at":          self.auto_reset_at,
                    "seconds_remaining": (max(0, int(self.auto_reset_at - time.time()))
                                          if self.auto_reset_at else None),
                    "window_seconds":    self.auto_reset_seconds,
                    "last_action":       self.auto_reset_last_action,
                },
                "branding": {
                    "client_name": config.CLIENT_NAME,
                    "tagline":     config.CLIENT_TAGLINE,
                },
                "thresholds": {
                    "throughput_pct":  config.THROUGHPUT_THRESHOLD_PCT,
                    "throughput_for":  config.THROUGHPUT_THRESHOLD_FOR,
                    "throughput_abs":  config.THROUGHPUT_THRESHOLD_OPS,
                    "memory_pct":      config.MEMORY_THRESHOLD_PCT,
                    "memory_for":      config.MEMORY_THRESHOLD_FOR,
                },
            }

    # ------------------------------------------------------------------ fetchers
    def _curl(self, url: str, headers: list[str] | None = None, timeout: int = 8) -> tuple[str, str]:
        cmd = ["curl", "-sS", "--max-time", str(timeout), "-k"]
        for h in headers or []:
            cmd += ["-H", h]
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
            if r.returncode != 0:
                return "", (r.stderr.strip() or f"exit {r.returncode}")[:120]
            return r.stdout, ""
        except subprocess.TimeoutExpired:
            return "", "timeout"
        except Exception as e:
            return "", f"{type(e).__name__}: {e}"[:120]

    def fetch_db(self) -> None:
        body, err = self._curl(
            f"{config.REDIS_CLOUD_API_BASE}/subscriptions/{config.REDIS_CLOUD_SUBSCRIPTION_ID}/databases",
            headers=[
                f"x-api-key: {config.REDIS_CLOUD_ACCOUNT_KEY}",
                f"x-api-secret-key: {config.REDIS_CLOUD_API_KEY}",
            ],
        )
        if err or not body:
            with self._lock:
                self.db_fetch_err = err or "empty"
            return
        try:
            data = json.loads(body)
            for db in data["subscription"][0]["databases"]:
                if int(db["databaseId"]) == config.DB_ID:
                    with self._lock:
                        self.db_status      = db.get("status", "?")
                        self.db_name        = db.get("name") or ""
                        self.db_throughput  = int(db["throughputMeasurement"]["value"])
                        self.db_memlim_gb   = float(db["memoryLimitInGb"])
                        self.db_replication = bool(db.get("replication", False))
                        self.db_modified    = db.get("lastModified") or ""
                        self.db_fetch_err   = ""
                    return
            with self._lock:
                self.db_fetch_err = f"dbId {config.DB_ID} not in response"
        except Exception as e:
            with self._lock:
                self.db_fetch_err = f"parse: {e}"[:120]

    def fetch_prom(self) -> None:
        err = ""
        # shards
        body, e = self._curl(f"{config.PROMETHEUS_URL}/api/v1/query?query=bdb_shards_used", timeout=3)
        if not e and body:
            try:
                v = json.loads(body)["data"]["result"]
                if v:
                    with self._lock:
                        self.db_shards = int(float(v[0]["value"][1]))
            except Exception:
                pass

        # live metrics
        for q, attr in (("bdb_instantaneous_ops_per_sec", "live_ops"),
                        ("bdb_used_memory",               "live_mem_bytes")):
            body, e = self._curl(f"{config.PROMETHEUS_URL}/api/v1/query?query={q}", timeout=3)
            if e:
                err = e
                continue
            try:
                v = json.loads(body)["data"]["result"]
                if v:
                    with self._lock:
                        setattr(self, attr, float(v[0]["value"][1]))
            except Exception as pe:
                err = f"parse {q}: {pe}"[:120]

        # rules / alerts
        body, e = self._curl(f"{config.PROMETHEUS_URL}/api/v1/rules", timeout=3)
        if not e and body:
            try:
                alerts: list[dict[str, str]] = []
                for g in json.loads(body).get("data", {}).get("groups", []):
                    for r in g.get("rules", []):
                        if r.get("type") != "alerting":
                            continue
                        active = r.get("alerts", [])
                        alerts.append({
                            "name":         r["name"],
                            "state":        r.get("state", "unknown"),
                            "active_since": active[0].get("activeAt", "") if active else "",
                        })
                with self._lock:
                    self.alerts = alerts
            except Exception as pe:
                err = f"parse rules: {pe}"[:120]
        elif e:
            err = e

        with self._lock:
            self.prom_fetch_err = err

    _EVENT_PATTERNS = ("Scaling database", "Saving task",
                       "Alert silenced successfully", "Received alert")

    def fetch_events(self) -> None:
        """Tail the autoscaler container logs via the Docker socket.

        We hit /var/run/docker.sock directly with stdlib HTTP — no
        `docker` CLI inside the image. Two reasons:

        1. The Debian `docker.io` package ships a CLI that negotiates
           API v1.41, which gets rejected by daemons that require
           v1.44+ (Ubuntu 24.04 / fresh Docker installs). The container
           card silently stayed empty until we noticed.
        2. Skipping the CLI binary saves ~120 MB from the runtime image.

        Requires the socket mounted read-only (already the case).
        """
        raw = _docker_logs_via_socket("autoscaler", tail=200, timeout=4)
        if raw is None:
            # Couldn't reach the socket at all — surface it so users debug.
            with self._lock:
                self.events = deque(
                    [{"ts": "", "kind": "error",
                      "msg": "Cannot read autoscaler logs (docker.sock unreachable)"}],
                    maxlen=50,
                )
            return

        parsed: list[dict[str, str]] = []
        for line in raw.splitlines():
            if not any(p in line for p in self._EVENT_PATTERNS):
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            ts = " ".join(parts[:2])

            kind, msg = "", ""
            if "Scaling database" in line:
                seg = line.split("Scaling database", 1)[1]
                if "throughputMeasurement=ThroughputMeasurement" in seg and "value=" in seg:
                    val = seg.split("value=", 1)[1].split(")", 1)[0]
                    try:
                        kind = "scale_up_throughput" if int(float(val)) > config.BASELINE_OPS else "scale_down_throughput"
                    except Exception:
                        kind = "scale_up_throughput"
                    msg = f"Scaling throughput → {val} ops/sec"
                elif "datasetSizeInGb=" in seg:
                    val = seg.split("datasetSizeInGb=", 1)[1].split(",", 1)[0]
                    if val != "null":
                        kind = "scale_memory"
                        msg = f"Scaling memory → {val} GB"
                if not msg:
                    continue
            elif "Saving task" in line:
                tid = ""
                if "taskId=" in line:
                    tid = line.split("taskId=", 1)[1].split(",", 1)[0][:8]
                kind, msg = "task", f"Task queued ({tid})"
            elif "Alert silenced" in line:
                kind, msg = "silence", "Alert silenced (cool-down)"
            elif "Received alert" in line:
                an = "?"
                if '"alertname":"' in line:
                    an = line.split('"alertname":"', 1)[1].split('"', 1)[0]
                status = "firing" if '"status":"firing"' in line else "resolved"
                color = "webhook" if status == "firing" else "silence"
                kind, msg = color, f"Webhook received: {an} {status}"

            parsed.append({"ts": ts, "kind": kind, "msg": msg})

        # dedup
        seen, unique = set(), []
        for e in parsed:
            key = (e["ts"], e["msg"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)

        with self._lock:
            self.events = deque(unique[-50:], maxlen=50)

    def fetch_memtier(self) -> None:
        st = memtier.status()
        with self._lock:
            self.memtier_running = st["running"]
            self.memtier_status  = st["status"]
            if st.get("params"):
                self.memtier_params = st["params"]

    def _append_history(self) -> None:
        with self._lock:
            self.history.append({
                "t":           time.time(),
                "live":        self.live_ops,
                "configured":  float(self.db_throughput),
            })

    # ------------------------------------------------------------------ auto-reset
    def _dataset_size_gb(self) -> float:
        """The configured dataset size, normalizing for HA.

        Redis Cloud's REST API returns `memoryLimitInGb` already doubled when
        replication is enabled (because HA needs master + replica memory).
        The customer-facing "dataset size" in the console is half of that.
        We compare baselines against dataset size, not raw memory limit.

        We rely on the `replication` boolean returned by the REST API — not
        on a memlim/baseline ratio heuristic. The heuristic mis-detected HA
        after a reset (memlim == BASELINE_MEM_GB looked like "no HA" even
        when replication was still true).
        """
        if self.db_memlim_gb <= 0:
            return 0.0
        return self.db_memlim_gb / 2 if self.db_replication else self.db_memlim_gb

    def _is_scaled_above_baseline(self) -> bool:
        return (self.db_throughput > config.BASELINE_OPS or
                self._dataset_size_gb() > config.BASELINE_MEM_GB + 0.01)

    async def _maybe_manage_reset(self) -> None:
        with self._lock:
            scaled    = self._is_scaled_above_baseline()
            running   = self.memtier_running
            has_task  = self._auto_reset_task is not None

        # When the scheduled scale-down is suspended (AUTO_RESET_ENABLED=false,
        # or a non-positive window), the DB stays scaled until a manual reset.
        # We never arm a countdown, and we tear down any that was in flight.
        if not config.AUTO_RESET_ACTIVE:
            if has_task:
                await self._cancel_reset_locked("suspended")
            return

        if scaled and not running and not has_task:
            logger.info("auto-reset: scheduling (%ss)", self.auto_reset_seconds)
            with self._lock:
                self._auto_reset_task = asyncio.create_task(self._countdown_then_reset())
                self.auto_reset_at    = time.time() + self.auto_reset_seconds
                self.auto_reset_last_action = "scheduled"
        elif (running or not scaled) and has_task:
            await self._cancel_reset_locked("cancelled_by_state")

    async def _countdown_then_reset(self) -> None:
        try:
            with self._lock:
                deadline = self.auto_reset_at or 0
            await asyncio.sleep(max(0, deadline - time.time()))
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, memtier.stop)
            res = await loop.run_in_executor(None, admin.reset_to_baseline)
            with self._lock:
                self.auto_reset_last_action = "reset: " + (res.get("message", "") or "done")
        except asyncio.CancelledError:
            with self._lock:
                self.auto_reset_last_action = "cancelled"
            raise
        except Exception as e:
            logger.exception("auto-reset failed: %s", e)
            with self._lock:
                self.auto_reset_last_action = f"error: {e}"
        finally:
            with self._lock:
                self._auto_reset_task = None
                self.auto_reset_at = None

    async def _cancel_reset_locked(self, reason: str) -> None:
        with self._lock:
            t = self._auto_reset_task
            self._auto_reset_task = None
            self.auto_reset_at = None
            self.auto_reset_last_action = reason
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def cancel_auto_reset(self) -> dict[str, Any]:
        await self._cancel_reset_locked("cancelled_by_user")
        return {"ok": True, "message": "Auto-reset cancelled"}

    async def trigger_auto_reset_now(self) -> dict[str, Any]:
        await self._cancel_reset_locked("manual_trigger")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, memtier.stop)
        res = await loop.run_in_executor(None, admin.reset_to_baseline)
        with self._lock:
            self.auto_reset_last_action = "manual_reset: " + (res.get("message", "") or "done")
        return res

    # ------------------------------------------------------------------ run loop
    async def run_forever(self) -> None:
        loop = asyncio.get_event_loop()
        last_db = 0.0
        last_events = 0.0
        while True:
            now = time.monotonic()
            if now - last_db > 4:
                await loop.run_in_executor(None, self.fetch_db)
                last_db = now
            await loop.run_in_executor(None, self.fetch_prom)
            if now - last_events > 2:
                await loop.run_in_executor(None, self.fetch_events)
                await loop.run_in_executor(None, self.fetch_memtier)
                last_events = now
            self._append_history()
            await self._maybe_manage_reset()
            await asyncio.sleep(1.0)
