"""Boot-time setup.

Rendering Prometheus templates is handled by a separate `init-config` service
(an Alpine container) that runs once before Prometheus/Alertmanager start.
That breaks the dependency cycle between the UI and the data plane.

The UI's job at boot is to:
  1. Validate connectivity to the Redis Cloud REST API (fail-fast on bad creds).
  2. Wait for the autoscaler service to come up.
  3. (Re)register the two scaling rules idempotently.

Re-running is safe: rule registration deletes existing rules for this
dbId first, then re-creates them.
"""
from __future__ import annotations
import asyncio
import json
import logging
import subprocess
import time
import urllib.error
import urllib.request

from . import config

logger = logging.getLogger("bootstrap")


# --------------------------------------------------------------------------- config validation
def validate_config() -> dict[str, str | bool]:
    """Sanity-check the configuration BEFORE we start serving traffic.

    Returns a result dict with `ok` and either `details` or `error`.
    Bootstrap continues even on validation warnings — fail-fast only on
    things we can't recover from (missing required env vars are caught
    much earlier, by config.py at import time).
    """
    findings: list[str] = []
    warnings: list[str] = []

    # 1. Can we reach the REST API at all?
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "8",
             "-H", f"x-api-key: {config.REDIS_CLOUD_ACCOUNT_KEY}",
             "-H", f"x-api-secret-key: {config.REDIS_CLOUD_API_KEY}",
             f"{config.REDIS_CLOUD_API_BASE}/subscriptions/{config.REDIS_CLOUD_SUBSCRIPTION_ID}"],
            capture_output=True, text=True, timeout=12,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {"ok": False, "error": "REST API unreachable — check network from this container"}
        sub_data = json.loads(r.stdout)
        if "error" in sub_data:
            return {"ok": False, "error": f"REST API error: {sub_data['error']}"}
    except Exception as e:
        return {"ok": False, "error": f"REST API call failed: {e}"}

    # 2. Auto-discover prometheusEndpoint if user didn't set it
    auto_endpoint = sub_data.get("prometheusEndpoint", "")
    if auto_endpoint and ":" in auto_endpoint:
        auto_endpoint = auto_endpoint.rsplit(":", 1)[0]  # strip :8070

    if not config.REDIS_CLOUD_INTERNAL_ENDPOINT and auto_endpoint:
        # Patch the running config in-place so subsequent code sees it.
        # (init-config has already rendered Prometheus from envvars, so this
        # path is mostly for the UI's own diagnostics + future re-renders.)
        config.REDIS_CLOUD_INTERNAL_ENDPOINT = auto_endpoint
        findings.append(f"auto-discovered prometheusEndpoint: {auto_endpoint}")
    elif config.REDIS_CLOUD_INTERNAL_ENDPOINT and auto_endpoint and \
         config.REDIS_CLOUD_INTERNAL_ENDPOINT != auto_endpoint:
        warnings.append(
            f"REDIS_CLOUD_INTERNAL_ENDPOINT={config.REDIS_CLOUD_INTERNAL_ENDPOINT!r} "
            f"but API says {auto_endpoint!r} — Prometheus may not scrape correctly"
        )

    # 3. Verify the DB exists in this subscription
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "8",
             "-H", f"x-api-key: {config.REDIS_CLOUD_ACCOUNT_KEY}",
             "-H", f"x-api-secret-key: {config.REDIS_CLOUD_API_KEY}",
             f"{config.REDIS_CLOUD_API_BASE}/subscriptions/{config.REDIS_CLOUD_SUBSCRIPTION_ID}/databases"],
            capture_output=True, text=True, timeout=12,
        )
        dbs = json.loads(r.stdout)["subscription"][0]["databases"]
        db = next((x for x in dbs if int(x["databaseId"]) == config.DB_ID), None)
        if not db:
            return {"ok": False, "error": f"REDIS_CLOUD_DATABASE_ID={config.DB_ID} not found in subscription {config.REDIS_CLOUD_SUBSCRIPTION_ID}"}
        findings.append(f"DB OK: {db.get('name', '?')} · {db['throughputMeasurement']['value']} ops/sec · {db['memoryLimitInGb']} GB")
        # 4. Sanity-check BASELINE_OPS vs reality
        actual_ops = int(db["throughputMeasurement"]["value"])
        if abs(actual_ops - config.BASELINE_OPS) > 0:
            warnings.append(
                f"BASELINE_OPS={config.BASELINE_OPS} but DB is configured for {actual_ops} — "
                "thresholds may not align with what the customer sees"
            )
    except Exception as e:
        warnings.append(f"DB sanity check failed: {e}")

    for w in warnings:
        logger.warning("config: %s", w)
    for f in findings:
        logger.info("config: %s", f)
    return {"ok": True, "findings": findings, "warnings": warnings}


# --------------------------------------------------------------------------- HTTP helpers (urllib, no deps)
def _http(method: str, url: str, *, body: dict | None = None, timeout: float = 5.0) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode() if e.fp else ""
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- scaling rule registration
def _scaling_rules() -> list[dict]:
    """The rules we register, parameterized by .env values.

    Memory scaling is conditional on MEMORY_SCALING_ENABLED — by default
    the autoscaler only acts on throughput.
    """
    rules: list[dict] = [{
        "dbId":         str(config.DB_ID),
        "ruleType":     "IncreaseThroughput",
        "scaleType":    "Deterministic",
        "scaleValue":   config.BURST_OPS,
        "scaleCeiling": config.THROUGHPUT_CEILING,
        "triggerType":  "webhook",
    }]
    if config.MEMORY_SCALING_ENABLED:
        rules.append({
            "dbId":         str(config.DB_ID),
            "ruleType":     "IncreaseMemory",
            "scaleType":    "Step",
            "scaleValue":   config.MEMORY_STEP_GB,
            "scaleCeiling": config.MEMORY_CEILING_GB,
            "triggerType":  "webhook",
        })
    return rules


async def _wait_for_autoscaler(max_seconds: int = 90) -> bool:
    """Block until the autoscaler /rules endpoint responds 200."""
    loop = asyncio.get_event_loop()
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        status, _ = await loop.run_in_executor(
            None, lambda: _http("GET", f"{config.AUTOSCALER_URL}/rules", timeout=3)
        )
        if status == 200:
            return True
        await asyncio.sleep(2)
    return False


async def register_scaling_rules() -> dict:
    """Wait for the autoscaler, then (re)register the two rules.

    Idempotent: existing rules for our dbId are deleted first.
    """
    loop = asyncio.get_event_loop()
    if not await _wait_for_autoscaler():
        logger.warning("autoscaler did not respond in time — rules NOT registered")
        return {"ok": False, "message": "autoscaler unreachable"}

    # Fetch existing rules and delete the ones belonging to our dbId
    status, body = await loop.run_in_executor(
        None, lambda: _http("GET", f"{config.AUTOSCALER_URL}/rules", timeout=5)
    )
    if status == 200:
        try:
            for r in json.loads(body):
                if str(r.get("dbId")) == str(config.DB_ID):
                    rid = r["ruleId"]
                    await loop.run_in_executor(
                        None, lambda rid=rid: _http("DELETE", f"{config.AUTOSCALER_URL}/rules/{rid}", timeout=5)
                    )
                    logger.info("deleted existing rule %s", rid)
        except Exception as e:
            logger.warning("could not parse existing rules: %s", e)

    # Create the two rules
    created = []
    for rule in _scaling_rules():
        status, body = await loop.run_in_executor(
            None, lambda r=rule: _http("POST", f"{config.AUTOSCALER_URL}/rules", body=r, timeout=5)
        )
        if status in (200, 201):
            try:
                created.append(json.loads(body).get("ruleId", "?"))
                logger.info("registered %s", rule["ruleType"])
            except Exception:
                created.append("?")
        else:
            logger.error("failed to register %s: %s %s", rule["ruleType"], status, body[:120])

    return {"ok": len(created) == len(_scaling_rules()),
            "registered": created,
            "rules": _scaling_rules()}


# --------------------------------------------------------------------------- main entry
async def run() -> None:
    logger.info("=== bootstrap: validating config ===")
    loop = asyncio.get_event_loop()
    cfg = await loop.run_in_executor(None, validate_config)
    if not cfg.get("ok"):
        logger.error("CONFIG INVALID — %s", cfg.get("error"))
        # Continue anyway so the UI starts and the user sees a diagnostic;
        # but make it loud in the logs.
    logger.info("=== bootstrap: registering scaling rules ===")
    result = await register_scaling_rules()
    logger.info("bootstrap done: %s", result)
