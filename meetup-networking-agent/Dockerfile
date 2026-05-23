# syntax=docker/dockerfile:1.7
# ---------- build stage ----------
FROM python:3.12-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libpq-dev \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.4.30

WORKDIR /app

# Install the shared SDK first to keep its layer cacheable across agents.
COPY agents_shared /app/agents_shared
WORKDIR /app/agents_shared
RUN uv venv /opt/venv \
 && . /opt/venv/bin/activate \
 && uv pip install --no-cache --python /opt/venv/bin/python .

WORKDIR /app
COPY meetup-networking-agent/pyproject.toml /app/meetup-networking-agent/pyproject.toml
COPY meetup-networking-agent /app/meetup-networking-agent
COPY agents-db /app/agents-db

RUN . /opt/venv/bin/activate \
 && uv pip install --no-cache --python /opt/venv/bin/python /app/meetup-networking-agent

# ---------- runtime stage ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app/meetup-networking-agent:/app" \
    AGENT_PORT=8000 \
    HEALTH_PORT=8080
# NOTE: Railway / Render / Fly.io override the bound port via $PORT; the
# health server reads PORT first and falls back to HEALTH_PORT.

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
        curl \
        tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app/meetup-networking-agent /app/meetup-networking-agent
COPY --from=build /app/agents_shared /app/agents_shared
COPY --from=build /app/agents-db /app/agents-db

# Drop privileges.
USER app

EXPOSE 8000 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-$HEALTH_PORT}/readyz" || exit 1

# tini reaps zombies + forwards SIGTERM cleanly to the uAgent runtime,
# which lets Kubernetes do a clean graceful shutdown when scaling pods.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/meetup-networking-agent/agent.py"]
