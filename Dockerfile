FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && apt-get purge -y git && apt-get autoremove -y

COPY app ./app

# GHunt writes its session under $HOME/.malfrats; give the service user a home.
RUN useradd --system --uid 10001 --create-home --home-dir /home/api api \
 && mkdir -p /home/api/.malfrats/ghunt \
 && chown -R api:api /home/api
ENV HOME=/home/api \
    GHUNT_CREDS_PATH=/home/api/.malfrats/ghunt/creds.m
USER api

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
