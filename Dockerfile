# syntax=docker/dockerfile:1

# ---------- build ----------
FROM python:3.11-slim AS builder
WORKDIR /service
ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ---------- runtime ----------
FROM python:3.11-slim AS production
WORKDIR /service
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/service/.venv/bin:$PATH" \
    PORT=8002

RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --no-create-home appuser

COPY --from=builder /service/.venv ./.venv
COPY app/ ./app/

USER appuser
EXPOSE 8002

# Liveness on purpose, never readiness: tying the container healthcheck to the
# database would let a brief Mongo blip restart a process that was perfectly
# fine. Readiness is at /health/ready for humans and orchestrators.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/health')" \
    || exit 1

CMD ["python", "-m", "app"]
