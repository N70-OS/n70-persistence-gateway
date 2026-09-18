# syntax=docker/dockerfile:1
# N70 OS - Tenant Isolation Persistence Gateway
# Thin HTTP layer over firestore_client.py's per-tenant Firestore access.
# No persistent disk, no baked-in credentials.

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN --mount=type=secret,id=github_pat \
    git config --global url."https://$(cat /run/secrets/github_pat)@github.com/".insteadOf "https://github.com/" && \
    pip install --no-cache-dir -r requirements.txt && \
    git config --global --unset url."https://$(cat /run/secrets/github_pat)@github.com/".insteadOf

COPY app.py .

CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 app:app
