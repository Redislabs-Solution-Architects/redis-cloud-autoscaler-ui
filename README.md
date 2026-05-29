# Redis Cloud Autoscaler — Demo UI

A self-contained, plug-and-play web UI around the
[Redis Cloud Autoscaler](https://github.com/redis-field-engineering/redis-cloud-autoscaler):
generate load, watch the autoscaler react to throughput / memory thresholds,
and demonstrate elasticity to your customers.

> 🇧🇷 **Tutorial em PT-BR**: see [`TUTORIAL.pt-BR.md`](TUTORIAL.pt-BR.md) for
> a step-by-step deployment guide in Portuguese (intended for the
> customer's DevOps/SRE team).

> **Status:** demo / educational — not a supported Redis product.
> The autoscaler upstream is supported field-engineering software with
> customers in production; this repository is a presentation layer.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  ●  active                Dataset · 2.5 GB · with HA: 5 GB physical          │
│  Throughput  25 000 ops/sec  ─ baseline ─                                    │
│  Shards      2                                                               │
│                                                                              │
│  Alerts:  IncreaseThroughput ○ inactive   IncreaseMemory ○ inactive          │
│                                                                              │
│  ⏱  Scheduled scale-down       Status: ✓ at baseline                         │
│                                                                              │
│  Load generator   [Baseline] [Sustained burst] [Dual scale] [Memory fill]    │
│                   [▶ Start load]   [■ Stop load]                             │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Five-minute quickstart

You need: **Docker**, **docker compose v2**, a Redis Cloud **Pro** subscription
with API keys, and a database ID.

```bash
git clone https://github.com/gacerioni/redis-cloud-autoscaler-ui.git
cd redis-cloud-autoscaler-ui
cp .env.example .env
$EDITOR .env                   # fill in the 5 required fields
docker compose up -d
open http://localhost:8000
```

That's it. The UI boots, validates the configuration, auto-discovers the
Prometheus endpoint, registers the scaling rule with the autoscaler, and
starts streaming live state to your browser.

### Required `.env` fields (5)

| Variable | Where to find it |
|---|---|
| `REDIS_HOST_AND_PORT` | Console → your database → *Configuration* → private endpoint |
| `REDIS_PASSWORD` | Console → your database → *Security* |
| `REDIS_CLOUD_API_KEY` + `REDIS_CLOUD_ACCOUNT_KEY` | Console → *Access Management* → API Keys (User Key + Account Key respectively) |
| `REDIS_CLOUD_SUBSCRIPTION_ID` + `DEMO_DB_ID` | numeric IDs from the console URLs |

> `REDIS_CLOUD_INTERNAL_ENDPOINT` is **auto-discovered** at boot from the
> subscription's `prometheusEndpoint` — leave it empty unless you want to
> override.

Defaults for everything else (thresholds, baselines, ceilings) live in
`.env.example` and ship with sensible values for a 2.5 GB / 25 k ops/sec
shard (the smallest "high-throughput" tier). Adjust to match your DB.

> **Note on `BASELINE_MEM_GB`**: this is the *dataset size* as shown in the
> Redis Cloud console — not the `memoryLimitInGb` from the REST API. With HA
> enabled, the API value is 2× the dataset size (master + replica). The UI
> handles both and displays `Dataset: X GB · with HA: 2X GB physical`.

---

## What runs

A single `docker compose up -d` brings up five containers (one is a one-shot init):

| Container | Image | Why |
|---|---|---|
| `init-config` | `alpine` (transient) | renders Prometheus templates from `.env` once, then exits |
| `ui` | `gacerioni/redis-cloud-autoscaler-ui` | this repo · FastAPI + WebSocket + SPA + memtier_benchmark + redis-cli |
| `autoscaler` | `ghcr.io/redis-field-engineering/redis-cloud-autoscaler` | upstream · Spring Boot, receives alerts, calls the REST API |
| `prometheus` | `prom/prometheus` | scrapes the DB's native `:8070` metrics endpoint |
| `alertmanager` | `prom/alertmanager` | routes firing alerts to the autoscaler |

**Only the UI (`:8000`) is published to the host by default**. The other
three services stay on the internal compose network — the cleaner, safer
default. If you need to inspect Prometheus / Alertmanager / Autoscaler
directly, opt-in with the expose overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.expose.yml up -d
```

## Security

- **HTTP Basic Auth** on every UI route (including WebSocket): set
  `UI_AUTH_PASSWORD` in `.env`. Empty = open access. Browser prompts on
  first hit.
- **Memory scaling is OFF by default** (`MEMORY_SCALING_ENABLED=false`).
  Memory is shown as contextual info on the dashboard, but no
  `IncreaseMemory` alert/rule is created. Turn on only if you know the
  cost implications of scaling RAM.
- **Throughput cap at 40 k ops/sec by default** — covers real-world
  customer peaks with headroom but prevents runaway scale.

```
   Redis Cloud DB  ──/metrics──▶  Prometheus  ──eval──▶  Alertmanager
        ▲                                                      │
        │                                                      │ webhook
        │  REST API (PUT /databases/{id})                      ▼
        └──────────────────────────────────────────  Autoscaler (Java)
                                                         ▲
                                                         │ rules
                                                     UI (this repo)
```

The UI also serves as a controller:

- **Load generator** — runs `memtier_benchmark` against the DB (local subprocess)
- **Admin** — safe `FLUSHDB` (preserves the autoscaler's metadata), force-reset
  the DB to baseline, re-register scaling rules
- **Scheduled scale-down** — a backend timer that waits `AUTO_RESET_SECONDS`
  after a scale-up event and calls the REST API to bring the DB back to
  baseline (the autoscaler itself is **scale-up only** — see below)

---

## Scaling policy — what the UI configures for you

On startup, the UI reads `.env` and:

1. **Renders `prometheus/alert.rules`** with two rules:
   - `IncreaseThroughput`: fires when `bdb_instantaneous_ops_per_sec > BASELINE_OPS × THROUGHPUT_THRESHOLD_PCT/100`, sustained for `THROUGHPUT_THRESHOLD_FOR`
   - `IncreaseMemory`: fires when `bdb_used_memory / bdb_memory_limit × 100 > MEMORY_THRESHOLD_PCT`, sustained for `MEMORY_THRESHOLD_FOR`
2. **Registers two scaling rules** with the autoscaler (idempotently):
   - Throughput → `Deterministic`, scales to `BURST_OPS`, ceiling `THROUGHPUT_CEILING`
   - Memory → `Step` `+MEMORY_STEP_GB`, ceiling `MEMORY_CEILING_GB`

Change a value in `.env`, then:

```bash
docker compose restart ui prometheus
```

Five seconds later the new policy is live. No image rebuild, no scripts.

### Why no automatic scale-down

Reactive scale-down in production destabilizes clusters: traffic oscillating
around the threshold causes yo-yo migrations. This repository deliberately
implements scale-down as a **scheduled** action: when the DB is sitting above
baseline and there is no active load, a backend timer fires once and calls
the Redis Cloud REST API directly to bring it back. In real customer setups
this window is hours (often via cron); we keep it short here for a
reproducible demo. The UI displays a banner explaining the tradeoff.

---

## Public deployment (HTTPS via Caddy)

If you want a public URL with automatic Let's Encrypt TLS:

1. Point a DNS A/AAAA record at the host.
2. Open ports 80 and 443 on the host.
3. Fill `DEMO_DOMAIN` and `DEMO_EMAIL` in `.env`.
4. Bring up the stack with the overlay file:

```bash
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d
```

Caddy fetches and renews the cert automatically; no certbot, no cron.

---

## Customizing for another customer

Everything that varies between customer demos lives in `.env`:

```bash
DEMO_CLIENT_NAME=Customer Inc.
DEMO_TAGLINE=Black Friday peak traffic
BASELINE_OPS=100000
BURST_OPS=200000
BASELINE_MEM_GB=25
MEMORY_STEP_GB=25
MEMORY_CEILING_GB=100
```

Restart the UI and Prometheus, and the dashboard re-renders for the new
scenario. The load generator presets (`Baseline traffic`, `Sustained burst`,
`Dual scale`, `Memory fill`) auto-fit the form fields but every parameter is
editable in the UI.

---

## Files of interest

```
.
├── docker-compose.yml          full stack (default)
├── docker-compose.public.yml   overlay for Caddy + TLS
├── Dockerfile                  multi-stage: memtier build + slim Python runtime
├── .env.example                every knob documented
├── app/
│   ├── main.py                 FastAPI app
│   ├── bootstrap.py            renders Prometheus configs + registers rules at boot
│   ├── config.py               .env → typed settings
│   ├── state.py                background fetcher + auto-reset scheduler
│   ├── memtier.py              memtier_benchmark subprocess controller
│   ├── admin.py                safe FLUSHDB + force-reset
│   └── static/                 HTML + CSS + vanilla JS + Chart.js
├── prometheus/
│   ├── prometheus.template.yml templated; rendered at boot
│   ├── alert.rules.template    templated; rendered at boot
│   └── alertmanager.yml
└── deploy/caddy/Caddyfile      5-line TLS config
```

---

## Troubleshooting

| Symptom | Action |
|---|---|
| UI says `connecting…` forever | Check `docker compose logs ui` for boot errors |
| "DB API: 401" in the diagnostics row | `REDIS_CLOUD_API_KEY` / `ACCOUNT_KEY` swapped; they map to `x-api-secret-key` / `x-api-key` respectively |
| Prometheus target `rediscloud` red | `REDIS_CLOUD_INTERNAL_ENDPOINT` wrong, or your network can't reach it (PSC / VPC peering not in place) |
| Autoscaler not reacting to alerts | Open the Admin panel → *Reload rules*. Confirm `docker compose logs autoscaler` shows `Received alert` |
| "Database size is smaller than usage" when downsizing | Hit Admin → *FLUSHDB* (safe) before reducing the memory limit |

---

## License

MIT. Built for field-engineering demos around the
[Redis Cloud Autoscaler](https://github.com/redis-field-engineering/redis-cloud-autoscaler).
