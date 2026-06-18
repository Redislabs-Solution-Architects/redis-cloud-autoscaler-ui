"""Configuration loaded from the environment.

Every knob the customer cares about lives in .env — nothing is hard-coded
into the code or templates. The boot sequence reads these values to render
the Prometheus configuration and to register scaling rules with the
autoscaler service.
"""
from __future__ import annotations
import os
from typing import Any


# --------------------------------------------------------------------------- helpers
def _str(key: str, default: str | None = None, *, required: bool = False) -> str:
    v = os.environ.get(key, default)
    if required and not v:
        raise RuntimeError(f"Missing required env var: {key}")
    return v or ""


def _int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _first(*keys: str, required: bool = False) -> str:
    """First non-empty value among several env vars (for backward-compat
    aliases). Earlier keys win — list the preferred name first."""
    for k in keys:
        v = os.environ.get(k, "")
        if v:
            return v
    if required:
        raise RuntimeError(f"Missing required env var: one of {', '.join(keys)}")
    return ""


# --------------------------------------------------------------------------- DB under management
REDIS_HOST_AND_PORT = _str("REDIS_HOST_AND_PORT", required=True)
REDIS_PASSWORD      = _str("REDIS_PASSWORD",      required=True)
DB_HOST, DB_PORT    = REDIS_HOST_AND_PORT.split(":", 1)

# --------------------------------------------------------------------------- Redis Cloud REST API
REDIS_CLOUD_API_BASE        = _str("REDIS_CLOUD_API_BASE", "https://api.redislabs.com/v1")
REDIS_CLOUD_API_KEY         = _str("REDIS_CLOUD_API_KEY",         required=True)  # → x-api-secret-key
REDIS_CLOUD_ACCOUNT_KEY     = _str("REDIS_CLOUD_ACCOUNT_KEY",     required=True)  # → x-api-key
REDIS_CLOUD_SUBSCRIPTION_ID = _str("REDIS_CLOUD_SUBSCRIPTION_ID", required=True)
# Preferred name is REDIS_CLOUD_DATABASE_ID; DEMO_DB_ID kept as a backward-
# compatible alias so existing .env files keep working.
DB_ID                       = int(_first("REDIS_CLOUD_DATABASE_ID", "DEMO_DB_ID", required=True))

# --------------------------------------------------------------------------- Prometheus metrics endpoint
# Hostname-only (no port) of the *internal* Redis Cloud cluster endpoint.
# Auto-discovered at boot from the subscription's `prometheusEndpoint` field —
# the customer normally leaves this EMPTY. Only set it to override discovery
# (e.g. a PSC/peering hostname that differs from what the API returns).
REDIS_CLOUD_INTERNAL_ENDPOINT = _str("REDIS_CLOUD_INTERNAL_ENDPOINT", "")
# Port where Redis Cloud Pro exposes native Prometheus metrics. Default 8070;
# override only if a scenario uses a non-standard port.
REDIS_CLOUD_METRICS_PORT      = _str("REDIS_CLOUD_METRICS_PORT", "8070")

# --------------------------------------------------------------------------- Branding (per-customer)
CLIENT_NAME    = _str("DEMO_CLIENT_NAME", "Demo")
CLIENT_TAGLINE = _str("DEMO_TAGLINE",     "Redis Cloud elasticity in action")

# --------------------------------------------------------------------------- UI Basic Auth
# Soft protection so anyone who lands on the URL can't immediately fire
# admin actions. Leave UI_AUTH_PASSWORD empty to disable auth entirely.
UI_AUTH_USERNAME = _str("UI_AUTH_USERNAME", "admin")
UI_AUTH_PASSWORD = _str("UI_AUTH_PASSWORD", "")

# --------------------------------------------------------------------------- Feature flags
# Memory autoscaling is OFF by default — scaling RAM has direct $$ impact and
# we don't want to encourage it accidentally in a demo. Set to "true" to enable.
MEMORY_SCALING_ENABLED = _bool("MEMORY_SCALING_ENABLED", False)

# --------------------------------------------------------------------------- Scaling thresholds (WHEN to scale)
THROUGHPUT_THRESHOLD_PCT = _int("THROUGHPUT_THRESHOLD_PCT", 80)        # % of BASELINE_OPS
THROUGHPUT_THRESHOLD_FOR = _str("THROUGHPUT_THRESHOLD_FOR", "30s")     # PromQL duration
MEMORY_THRESHOLD_PCT     = _int("MEMORY_THRESHOLD_PCT",     80)        # % of memory limit
MEMORY_THRESHOLD_FOR     = _str("MEMORY_THRESHOLD_FOR",     "30s")

# --------------------------------------------------------------------------- Scaling targets (HOW to scale)
BASELINE_OPS       = _int("BASELINE_OPS",       25000)
BURST_OPS          = _int("BURST_OPS",          40000)
THROUGHPUT_CEILING = _int("THROUGHPUT_CEILING", 40000)
BASELINE_MEM_GB    = _float("BASELINE_MEM_GB",  2.5)
MEMORY_STEP_GB     = _float("MEMORY_STEP_GB",   2.0)
MEMORY_CEILING_GB  = _float("MEMORY_CEILING_GB", 5.0)

# Computed once at boot so prometheus alert.rules can use an absolute number.
THROUGHPUT_THRESHOLD_OPS = int(BASELINE_OPS * THROUGHPUT_THRESHOLD_PCT / 100)

# --------------------------------------------------------------------------- Scheduled scale-down
# AUTO_RESET_ENABLED=false suspends the scheduled scale-down entirely: a
# scaled-up DB stays up until someone resets it manually (UI "Reset now" or
# POST /api/admin/reset-baseline). Use this for real events where you want the
# DB to hold its scaled capacity for the whole window (e.g. a live match).
# A non-positive AUTO_RESET_SECONDS is also treated as disabled.
AUTO_RESET_ENABLED = _bool("AUTO_RESET_ENABLED", True)
AUTO_RESET_SECONDS = _int("AUTO_RESET_SECONDS", 300)
# Single source of truth for "is the scheduler armed at all".
AUTO_RESET_ACTIVE  = AUTO_RESET_ENABLED and AUTO_RESET_SECONDS > 0

# --------------------------------------------------------------------------- Internal service wiring
PROMETHEUS_URL       = _str("PROMETHEUS_URL",       "http://prometheus:9090")
AUTOSCALER_URL       = _str("AUTOSCALER_URL",       "http://autoscaler:8080")
ALERTMANAGER_URL     = _str("ALERTMANAGER_URL",     "http://alertmanager:9093")

# --------------------------------------------------------------------------- Memtier presets (load profiles)
# Threads are capped at 4 for predictability. The user can override any individual
# field through the UI form; the presets are just starting points.
PRESETS: list[dict[str, Any]] = [
    {
        "id": "warmup",
        "name": "Baseline traffic",
        "description": "Lightest preset — read-heavy cache pattern (may still cross thresholds on small databases)",
        "params": {"threads": 2, "clients": 25, "pipeline": 5,
                   "ratio": "1:10", "data_size": 256,
                   "key_minimum": 1, "key_maximum": 500_000,
                   "test_time": 300},
    },
    {
        "id": "kickoff",
        "name": "Sustained burst",
        "description": "Read-heavy burst that crosses the throughput threshold in seconds",
        "params": {"threads": 4, "clients": 60, "pipeline": 20,
                   "ratio": "1:10", "data_size": 256,
                   "key_minimum": 1, "key_maximum": 2_000_000,
                   "test_time": 600},
    },
    {
        "id": "surge",
        "name": "Dual scale",
        "description": "SET-heavy with novel keys — trips throughput AND memory thresholds",
        "params": {"threads": 4, "clients": 60, "pipeline": 25,
                   "ratio": "10:1", "data_size": 1024,
                   "key_minimum": 1, "key_maximum": 5_000_000,
                   "test_time": 600},
    },
    {
        "id": "memory_fill",
        "name": "Memory fill",
        "description": "Pure inserts with large values — exercises memory scale-up only",
        "params": {"threads": 4, "clients": 50, "pipeline": 30,
                   "ratio": "1:0", "data_size": 4096,
                   "key_minimum": 1, "key_maximum": 5_000_000,
                   "test_time": 900},
    },
]


def preset_by_id(pid: str) -> dict[str, Any] | None:
    return next((p for p in PRESETS if p["id"] == pid), None)


def build_memtier_argv(params: dict[str, Any]) -> list[str]:
    """memtier_benchmark argv (no plain TLS — the demo uses a plain endpoint
    inside a private network)."""
    return [
        "-s", DB_HOST, "-p", DB_PORT, "-a", REDIS_PASSWORD,
        "--hide-histogram",
        "-t", str(params["threads"]),
        "-c", str(params["clients"]),
        f"--pipeline={params['pipeline']}",
        f"--ratio={params['ratio']}",
        "--key-pattern=R:R",
        f"--key-minimum={params['key_minimum']}",
        f"--key-maximum={params['key_maximum']}",
        f"--data-size={params['data_size']}",
        f"--test-time={params['test_time']}",
    ]
