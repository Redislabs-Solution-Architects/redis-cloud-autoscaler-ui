# syntax=docker/dockerfile:1.6
# ─────────────────────────────────────────────────────────────────────────────
# Redis Cloud Autoscaler UI — multi-stage image.
#
# Stage 1 builds memtier_benchmark from source (needed inside the UI
# container so the "Load generator" panel works without docker-in-docker).
# Stage 2 is a slim Python runtime with memtier + redis-cli + docker CLI
# (read-only access to docker.sock is used to tail autoscaler logs).
# ─────────────────────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.12

FROM debian:bookworm-slim AS memtier-builder
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential autoconf automake libtool pkg-config \
        libevent-dev libpcre3-dev libssl-dev zlib1g-dev \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 https://github.com/RedisLabs/memtier_benchmark.git . \
    && autoreconf -ivf && ./configure && make -j"$(nproc)" \
    && strip memtier_benchmark


FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Runtime deps:
#  - libevent (+ openssl) / zlib / pcre → memtier_benchmark needs these at runtime
#  - redis-tools                        → safe FLUSHDB + DBSIZE
#  - curl / jq                          → REST API calls + diagnostics
#  - docker.io (CLI only)               → docker logs autoscaler (sock mounted ro)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libevent-2.1-7 libevent-openssl-2.1-7 libssl3 zlib1g libpcre3 \
        redis-tools curl jq ca-certificates \
        docker.io \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man

# Smoke-test memtier so the build fails immediately if a shared library is missing.
RUN memtier_benchmark --version

COPY --from=memtier-builder /src/memtier_benchmark /usr/local/bin/memtier_benchmark

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app /app/app
COPY prometheus /app/prometheus

# OCI labels for the registry
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="redis-cloud-autoscaler-ui" \
      org.opencontainers.image.description="Web UI + bootstrap layer around the Redis Cloud Autoscaler" \
      org.opencontainers.image.source="https://github.com/gacerioni/redis-cloud-autoscaler-ui" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
