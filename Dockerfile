# syntax=docker/dockerfile:1

# Versiones fijas: la misma imagen base (por digest) en las dos etapas y un uv
# concreto, para que dos builds del mismo commit den el mismo resultado.
# Para actualizar: cambiar el tag y el digest juntos.
ARG PYTHON_IMAGE=python:3.11-slim@sha256:064f01707f2fc8b06bae86bb43875266c6e41bac7994e77ec5dc623c4a4d0545
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.7

FROM ${UV_IMAGE} AS uv

# ---------- build ----------
FROM ${PYTHON_IMAGE} AS builder
WORKDIR /service
ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ---------- runtime ----------
FROM ${PYTHON_IMAGE} AS production
WORKDIR /service
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/service/.venv/bin:$PATH" \
    PORT=8002

# El zlib1g de la base arrastra dos CVE high con fix ya publicado en Debian
# (CVE-2026-76844 y CVE-2026-85091, las dos en la API gz_* de archivos gzip).
# Se instala la version parcheada con la version fijada: un "apt-get upgrade" a
# secas traeria lo que haya en el mirror el dia del build y dos builds del mismo
# commit dejarian de dar la misma imagen; escrita aca, sigue siendo reproducible
# igual que el digest del ARG PYTHON_IMAGE.
#
# Para actualizarla:
#   docker run --rm <PYTHON_IMAGE> sh -c "apt-get update -qq && apt-cache policy zlib1g"
# y copiar el "Candidate". Si Debian retira esta version del mirror el build
# falla, y es una senal deseada: hay que volver a mirar el paquete en vez de
# arrastrar una version que ya no existe.
RUN apt-get update && \
    apt-get install --no-install-recommends -y \
        zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 && \
    rm -rf /var/lib/apt/lists/*

# El servicio corre del venv (/service/.venv) y no instala nada en runtime: pip,
# setuptools y wheel no hacen falta en la imagen final. Se van, y con ellos las
# copias vendorizadas en setuptools/_vendor (wheel y jaraco.context), que son
# otros dos high. Menos en la imagen, menos que escanear.
RUN rm -rf /usr/local/lib/python3.11/site-packages/pip* \
           /usr/local/lib/python3.11/site-packages/setuptools* \
           /usr/local/lib/python3.11/site-packages/wheel* \
           /usr/local/bin/pip* \
           /usr/local/bin/wheel

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
