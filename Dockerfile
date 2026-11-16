ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

FROM ${PYTHON_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

FROM base AS dependencies

COPY requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/venv \
    && python -m pip --python /opt/venv install \
        --require-hashes --only-binary=:all: -r /tmp/requirements.lock

FROM base AS runtime

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --no-log-init --create-home appuser

COPY --from=dependencies /opt/venv /opt/venv
COPY --chown=1000:1000 app/ /app/app/

ENV UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=8001 \
    UVICORN_ACCESS_LOG=false \
    UVICORN_PROXY_HEADERS=false \
    UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=60

USER 1000:1000
EXPOSE 8001
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health/ready', timeout=3).close()"]

CMD ["python", "-m", "uvicorn", "app.main:app"]
