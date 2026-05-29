<div align="center">

# Redis Cloud Autoscaler — Web UI

**A plug-and-play orchestration + dashboard layer around the official Redis Cloud Autoscaler.**
One `docker compose up -d` away from watching your Redis Cloud Pro database elastically scale, end to end.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/gacerioni/redis-cloud-autoscaler-ui)](https://hub.docker.com/r/gacerioni/redis-cloud-autoscaler-ui)
[![Image Size](https://img.shields.io/docker/image-size/gacerioni/redis-cloud-autoscaler-ui/latest)](https://hub.docker.com/r/gacerioni/redis-cloud-autoscaler-ui/tags)
[![Multi-arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-informational)](https://hub.docker.com/r/gacerioni/redis-cloud-autoscaler-ui/tags)
[![CI](https://img.shields.io/github/actions/workflow/status/Redislabs-Solution-Architects/redis-cloud-autoscaler-ui/ci.yml?branch=main)](.github/workflows/ci.yml)

[Quickstart](#-five-minute-quickstart) ·
[Architecture](#%EF%B8%8F-architecture) ·
[Configuration](#-configuration) ·
[Security](#-security--defaults) ·
[Tutorial 🇧🇷](TUTORIAL.pt-BR.md)

</div>

---

## What this is

The [**Redis Cloud Autoscaler**](https://github.com/redis-field-engineering/redis-cloud-autoscaler)
is a Spring Boot service maintained by Redis Field Engineering. It listens for
Prometheus alerts and calls the Redis Cloud REST API to scale databases.
**Powerful, production-grade, but bring-your-own-everything**: you have to
stand up Prometheus, Alertmanager, write the alert rules, register the
scaling rules, and build a UI if you want one.

This repository **wraps that autoscaler in a self-contained demo stack** so
you can show it working in under five minutes — to customers, to your team,
or to yourself.

| | Upstream autoscaler (alone) | This repo |
|---|---|---|
| Java Spring Boot service | ✅ | ✅ *(used as-is, unchanged)* |
| Prometheus + scrape config | bring-your-own | **auto-rendered from `.env`** |
| Alertmanager + routes | bring-your-own | **auto-rendered from `.env`** |
| Alert rules (`IncreaseThroughput` …) | hand-written | **derived from thresholds in `.env`** |
| Scaling rule registration | hand-curled into the API | **idempotent on every boot** |
| Discovery of the internal Prometheus endpoint | manual lookup | **auto-discovered from the subscription** |
| Web dashboard | — | ✅ FastAPI + WebSocket + Chart.js SPA |
| Load generator | — | ✅ `memtier_benchmark` shipped inside the image |
| Scheduled scale-down | — | ✅ backend timer + Redis Cloud REST API |
| Safe `FLUSHDB` (preserves autoscaler metadata) | — | ✅ |
| HTTP Basic Auth + WebSocket auth | — | ✅ optional, single env var |
| HTTPS via Caddy (Let's Encrypt) | — | ✅ opt-in overlay |
| Multi-arch image (`amd64` + `arm64`) | — | ✅ |

> 📌 **Status:** demo / educational. The upstream autoscaler is field-supported software with production customers; **this repository is the presentation + plug-and-play layer around it**, not an officially supported Redis product.

---

## ⚡ Five-minute quickstart

You need: **Docker**, **docker compose v2**, a **Redis Cloud Pro** subscription
with API keys and a database, and **network reachability** from this host to
the database's private endpoint (PSC, VPC peering, Transit Gateway, …).

```bash
git clone https://github.com/Redislabs-Solution-Architects/redis-cloud-autoscaler-ui.git
cd redis-cloud-autoscaler-ui
cp .env.example .env
$EDITOR .env                   # fill in 5 required fields (see below)
docker compose up -d
open http://localhost:8000     # the UI
```

That's it. The stack:

1. **Renders Prometheus + alert rules** from your `.env` (Alpine init container, runs once).
2. **Auto-discovers** the internal Prometheus scrape endpoint from your subscription.
3. **Starts** Prometheus, Alertmanager, the upstream Autoscaler, and this UI.
4. **Registers** the scaling rule with the Autoscaler — idempotently.
5. **Streams live state** to your browser over WebSocket.

### Required `.env` fields (5)

| Variable | Where to find it |
|---|---|
| `REDIS_HOST_AND_PORT` | Console → your database → *Configuration* → private endpoint |
| `REDIS_PASSWORD` | Console → your database → *Security* |
| `REDIS_CLOUD_API_KEY` + `REDIS_CLOUD_ACCOUNT_KEY` | Console → *Access Management* → API Keys *(User Key + Account Key respectively)* |
| `REDIS_CLOUD_SUBSCRIPTION_ID` + `DEMO_DB_ID` | numeric IDs from the console URLs |

Everything else (thresholds, baselines, cap, branding, scale-down window, auth) ships with sensible defaults — see [`.env.example`](.env.example).

> 🇧🇷 **Tutorial em português** com passo a passo, screenshots e troubleshooting: [`TUTORIAL.pt-BR.md`](TUTORIAL.pt-BR.md)

---

## 🏗️ Architecture

```mermaid
flowchart LR
    subgraph YourNetwork["your VPC / VM"]
        direction LR
        UI["🌐 <b>UI</b><br/>FastAPI · WebSocket<br/>SPA · memtier"]:::ui
        P["📊 <b>Prometheus</b><br/>scrape :8070"]:::prom
        A["🚨 <b>Alertmanager</b><br/>route → webhook"]:::prom
        S["⚙️ <b>Autoscaler</b><br/>Spring Boot<br/>(upstream)"]:::auto
    end

    DB[("🔴 <b>Redis Cloud Pro</b><br/>database<br/><i>managed</i>")]:::redis

    DB -- "metrics scrape" --> P
    P  -- "rule fires" --> A
    A  -- "webhook POST" --> S
    S  == "PUT /databases/{id}<br/>(REST API · scales the DB)" ==> DB

    UI -. "register scaling rules" .-> S
    UI -. "read state" .-> P
    UI == "scheduled scale-down<br/>(REST API)" ==> DB
    UI -- "generate load" --> DB

    classDef redis fill:#DC382D,color:#fff,stroke:#7c1d14,stroke-width:2px
    classDef prom  fill:#E6522C,color:#fff,stroke:#8a2a13,stroke-width:1px
    classDef auto  fill:#6DB33F,color:#fff,stroke:#3c5f23,stroke-width:1px
    classDef ui    fill:#3b82f6,color:#fff,stroke:#1d4ed8,stroke-width:1px
```

**The thick lines (`==>`) are write operations on your Redis Cloud DB**: the autoscaler scales it up reactively, and the UI's scheduled-reset timer scales it back down on its own clock.

### What runs

| Container | Image | Lifecycle | Why |
|---|---|---|---|
| `init-config` | `alpine:3.20` | one-shot | renders Prometheus templates + auto-discovers metrics endpoint, then exits |
| `autoscaler` | `ghcr.io/redis-field-engineering/redis-cloud-autoscaler` | long-running | the unchanged upstream service |
| `prometheus` | `prom/prometheus` | long-running | scrapes `bdb_*` metrics from the DB's `:8070` endpoint |
| `alertmanager` | `prom/alertmanager` | long-running | routes `IncreaseThroughput` (and optionally `IncreaseMemory`) webhooks |
| `ui` | `gacerioni/redis-cloud-autoscaler-ui` | long-running | this repo — FastAPI + Chart.js dashboard, load generator, admin actions |

**Only the UI (`:8000`) is published to the host by default.** Prometheus / Alertmanager / Autoscaler stay on the internal compose network. To inspect them directly, opt-in:

```bash
docker compose -f docker-compose.yml -f docker-compose.expose.yml up -d
```

---

## 🔧 Configuration

Everything is in [`.env`](.env.example). Quick map:

```bash
# WHEN to scale (thresholds + debounce)
THROUGHPUT_THRESHOLD_PCT=80       # of BASELINE_OPS
THROUGHPUT_THRESHOLD_FOR=30s
MEMORY_THRESHOLD_PCT=80           # only used if MEMORY_SCALING_ENABLED=true
MEMORY_THRESHOLD_FOR=30s

# HOW MUCH to scale (targets + hard caps)
BASELINE_OPS=25000
BURST_OPS=40000                   # IncreaseThroughput → jumps to this
THROUGHPUT_CEILING=40000          # never beyond this
BASELINE_MEM_GB=2.5
MEMORY_STEP_GB=2                  # +N GB per memory trigger
MEMORY_CEILING_GB=5

# Scheduled scale-down window
AUTO_RESET_SECONDS=300            # back to baseline N seconds after a scale-up
```

Change anything, then:

```bash
docker compose down && docker compose up -d
```

Five seconds later the new policy is live — the init container re-renders the
Prometheus rules and the UI re-registers the scaling rules with the autoscaler.

### Per-customer demo branding

```bash
DEMO_CLIENT_NAME="Customer Inc."
DEMO_TAGLINE="Black Friday peak traffic"
```

These show up in the dashboard header.

---

## 🛡️ Security & defaults

| Default | Why |
|---|---|
| **HTTP Basic Auth** off (`UI_AUTH_PASSWORD=`) | open access for quick demos; set any non-empty value to enable. Browser prompts the first time; the WebSocket upgrade carries the same credentials. |
| **Memory scaling** off (`MEMORY_SCALING_ENABLED=false`) | scaling memory has direct cost impact. The dashboard still shows live memory usage as context, but no `IncreaseMemory` alert/rule is created. |
| **Throughput cap** at `40 000 ops/sec` | covers typical event-driven peaks (live sports / live streaming / voting events around 30 k ops/sec) with headroom, while preventing runaway scale. |
| **Internal ports** unpublished | only `:8000` (UI) is on the host. Prometheus/Alertmanager/Autoscaler are reachable only from inside the compose network. |
| **Reactive scale-down** disabled by design | yo-yo migrations destabilize clusters in production. The UI runs a one-shot timer per scale-up event and calls the REST API directly — independently of the autoscaler. |

---

## 🌐 Public deployment with HTTPS

For a public URL with automatic Let's Encrypt certs (via [Caddy](https://caddyserver.com/)):

```bash
# 1. point a DNS A/AAAA at this host
# 2. open :80 and :443
# 3. in .env:
DEMO_DOMAIN=autoscaler.yourdomain.com
DEMO_EMAIL=ops@yourdomain.com
# 4. bring up with the overlay:
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d
```

Caddy fetches and renews the certificate automatically. No certbot, no cron.

---

## 📁 Repo layout

```
.
├── docker-compose.yml            full stack (default — only UI exposed)
├── docker-compose.public.yml     overlay · Caddy + Let's Encrypt
├── docker-compose.expose.yml     overlay · publish internal ports (debug)
├── Dockerfile                    multi-stage · memtier source build + slim runtime
├── .env.example                  every knob, documented
│
├── app/                          this repo's contribution
│   ├── main.py                   FastAPI + WebSocket + Basic Auth middleware
│   ├── bootstrap.py              boot: validate config + register scaling rules
│   ├── config.py                 typed env-var settings
│   ├── state.py                  background fetcher + auto-reset scheduler
│   ├── memtier.py                memtier_benchmark subprocess controller
│   ├── admin.py                  safe FLUSHDB · force-reset · reload-rules
│   └── static/                   HTML · CSS · vanilla JS · Chart.js · logos
│
├── prometheus/                   templates (rendered into a shared volume at boot)
│   ├── prometheus.template.yml
│   ├── alert.rules.template               IncreaseThroughput (always)
│   ├── alert.rules.memory.template        IncreaseMemory (only if enabled)
│   └── alertmanager.yml
│
├── deploy/caddy/Caddyfile        5-line TLS config
└── .github/workflows/ci.yml      Python syntax + compose validation + multi-arch build
```

---

## 🩹 Troubleshooting

| Symptom | Action |
|---|---|
| UI says `connecting…` forever | `docker compose logs ui` — check for bootstrap errors |
| **`DB API: 401`** in the diagnostics row | `REDIS_CLOUD_API_KEY` ⇄ `REDIS_CLOUD_ACCOUNT_KEY` swapped *(they map to `x-api-secret-key` / `x-api-key` respectively)* |
| Prometheus target `rediscloud` red | network can't reach `<endpoint>:8070` — fix PSC / VPC peering before retrying |
| Autoscaler not reacting to alerts | Open the **Admin** panel → *Reload rules*. Confirm `docker compose logs autoscaler` shows `Received alert` |
| `Database size is smaller than usage` when downsizing | Admin → *FLUSHDB* (safe — preserves the autoscaler's metadata) before reducing memory |
| Want to inspect Prometheus directly | use the `docker-compose.expose.yml` overlay |

---

## 🔗 References

- [**redis-field-engineering/redis-cloud-autoscaler**](https://github.com/redis-field-engineering/redis-cloud-autoscaler) — the upstream Java service this repo wraps
- [Redis Cloud REST API](https://api.redislabs.com/v1/swagger-ui/index.html)
- [memtier_benchmark](https://github.com/RedisLabs/memtier_benchmark) — the load generator we ship inside the UI image
- [Prometheus](https://prometheus.io/) · [Alertmanager](https://prometheus.io/docs/alerting/latest/alertmanager/) · [Caddy](https://caddyserver.com/)

---

<div align="center">
<sub>MIT-licensed. Built for Redis Field Engineering / Solutions Architects demos.<br/>
Maintained by <a href="https://github.com/gacerioni">@gacerioni</a> · PRs welcome.</sub>
</div>
