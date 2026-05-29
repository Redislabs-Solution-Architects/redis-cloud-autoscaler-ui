"""Admin actions: safe FLUSHDB + force-reset DB to baseline."""
from __future__ import annotations
import json
import logging
import shutil
import subprocess
from typing import Any

from . import config

logger = logging.getLogger("admin")

# Keys with this prefix are the autoscaler's Rule/Task documents — preserve
# them so a FLUSHDB doesn't kneecap the scaling logic.
_AUTOSCALER_PREFIX = "com.redis.autoscaler."


def _run(cmd: list[str], timeout: int = 15) -> tuple[bool, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except Exception as e:
        return False, "", f"{type(e).__name__}: {e}"


def _redis_cli(args: list[str], timeout: int = 8) -> tuple[bool, str]:
    if not shutil.which("redis-cli"):
        return False, "redis-cli not installed in container"
    base = [
        "-h", config.DB_HOST, "-p", config.DB_PORT,
        "-a", config.REDIS_PASSWORD, "--no-auth-warning",
    ]
    ok, out, err = _run(["redis-cli", *base, *args], timeout=timeout)
    return ok, (out if ok else (err or out))


def flushdb() -> dict[str, Any]:
    """Wipe customer keys; preserve any com.redis.autoscaler.* documents."""
    ok, before = _redis_cli(["DBSIZE"], timeout=4)
    try:
        n_before = int(before.split()[-1])
    except Exception:
        n_before = -1

    # SCAN keys → exclude autoscaler prefix → UNLINK in batches of 500.
    script = (
        f"redis-cli -h {config.DB_HOST} -p {config.DB_PORT} -a '{config.REDIS_PASSWORD}' "
        f"--no-auth-warning --scan "
        f"| grep -v '^{_AUTOSCALER_PREFIX}' "
        f"| xargs -r -n 500 redis-cli -h {config.DB_HOST} -p {config.DB_PORT} "
        f"-a '{config.REDIS_PASSWORD}' --no-auth-warning UNLINK"
    )
    ok, _, err = _run(["bash", "-c", script], timeout=60)
    if not ok:
        return {"ok": False, "message": (err or "flush failed")[:200]}

    ok, after = _redis_cli(["DBSIZE"], timeout=4)
    try:
        n_after = int(after.split()[-1])
    except Exception:
        n_after = -1

    if n_before >= 0 and n_after >= 0:
        wiped = max(0, n_before - n_after)
        return {"ok": True, "message": f"Wiped {wiped:,} keys"}
    return {"ok": True, "message": "Flushed customer keys"}


def _is_ha_enabled() -> bool:
    """Ask the REST API whether `replication` is on for this DB.

    Returns False on any error — safer than guessing True (we'd then send
    2× the dataset and grow the DB on a reset; sending 1× when HA is
    actually on just shrinks it, which the caller will notice).
    """
    url = (f"{config.REDIS_CLOUD_API_BASE}/subscriptions/"
           f"{config.REDIS_CLOUD_SUBSCRIPTION_ID}/databases/{config.DB_ID}")
    ok, out, _ = _run([
        "curl", "-sS", "--max-time", "8",
        "-H", f"x-api-key: {config.REDIS_CLOUD_ACCOUNT_KEY}",
        "-H", f"x-api-secret-key: {config.REDIS_CLOUD_API_KEY}",
        url,
    ], timeout=10)
    if not ok:
        logger.warning("could not fetch DB to determine HA: assuming False")
        return False
    try:
        return bool(json.loads(out).get("replication", False))
    except Exception:
        logger.warning("malformed DB response when checking HA: assuming False")
        return False


def reset_to_baseline() -> dict[str, Any]:
    """PUT the DB back to baseline via the REST API.

    Two design rules baked in:

    1. **Throughput is always reset** — that's the whole point of the demo.
    2. **Memory is ONLY touched when `MEMORY_SCALING_ENABLED=true`.** If the
       operator opted out of memory scaling, the autoscaler never grew the
       memlim in the first place, so this reset must not shrink it either.
       Toggling memory limits as a side-effect of a throughput reset would
       be a footgun (and is what got us here in the first place — see the
       2.5 GB-instead-of-5 GB regression on 2026-05-29).

    When memory IS reset, we must honor HA: Redis Cloud's REST API expects
    `memoryLimitInGb` as the *physical* size (master + replica when HA is
    on), which is 2 × the dataset size shown in the console. The
    `replication` boolean from the API is the source of truth — never
    inferred from memlim/baseline ratios.
    """
    payload: dict[str, Any] = {
        "throughputMeasurement": {
            "by": "operations-per-second",
            "value": config.BASELINE_OPS,
        },
    }
    mem_note = ""
    if config.MEMORY_SCALING_ENABLED:
        # Pull the live `replication` flag from the REST API directly. We
        # could read it off the in-process state singleton, but that'd
        # introduce a circular import — and the cost of one extra GET is
        # noise compared to the PUT we're about to make.
        ha = _is_ha_enabled()
        phys_gb = float(config.BASELINE_MEM_GB) * (2 if ha else 1)
        payload["memoryLimitInGb"] = phys_gb
        mem_note = (f" · {config.BASELINE_MEM_GB} GB dataset"
                    f" ({phys_gb} GB physical{' with HA' if ha else ''})")
    url = (f"{config.REDIS_CLOUD_API_BASE}/subscriptions/"
           f"{config.REDIS_CLOUD_SUBSCRIPTION_ID}/databases/{config.DB_ID}")
    ok, out, err = _run([
        "curl", "-sS", "--max-time", "12", "-X", "PUT",
        "-H", f"x-api-key: {config.REDIS_CLOUD_ACCOUNT_KEY}",
        "-H", f"x-api-secret-key: {config.REDIS_CLOUD_API_KEY}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload),
        url,
    ], timeout=15)
    if not ok:
        return {"ok": False, "message": (err or out)[:300]}
    msg = f"Scale request submitted (back to {config.BASELINE_OPS:,} ops/sec{mem_note})"
    try:
        data = json.loads(out)
        tid = data.get("taskId") or ""
        if tid:
            msg += f" · task {str(tid)[:8]}"
    except Exception:
        pass
    return {"ok": True, "message": msg}


def reload_scaling_rules() -> dict[str, Any]:
    """Idempotent rule re-register (useful if someone wiped the autoscaler's storage)."""
    from . import bootstrap
    import asyncio
    try:
        return asyncio.run(bootstrap.register_scaling_rules())
    except RuntimeError:
        # If called from within a running loop, fall back to a thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(lambda: asyncio.run(bootstrap.register_scaling_rules())).result()
