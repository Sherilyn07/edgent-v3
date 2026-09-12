# The ingest worker's own image — deployed as its own ECS Fargate service,
# independent of the API and of chat-worker.
#
# Build from the v3 repo root so `shared/` is in context:
#   docker build -f workers/ingest/Dockerfile -t edgentrag/ingest-worker .

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    # Docling caches its models here. Mount ephemeral storage generously (the
    # ECS task definition should give this task real disk — see DEPLOY.md) so
    # repeated cold starts are not repeated multi-hundred-MB downloads.
    HF_HOME=/home/app/.cache/huggingface

WORKDIR /app

# Docling needs a few native libraries the slim image does not carry.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY shared/requirements.txt /app/shared/requirements.txt
COPY workers/ingest/requirements.txt /app/workers/ingest/requirements.txt
RUN pip install -r shared/requirements.txt -r workers/ingest/requirements.txt

COPY shared /app/shared
COPY workers/ingest /app/workers/ingest

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /home/app/.cache/huggingface \
    && chown -R app:app /app /home/app
USER app

# No port, no HEALTHCHECK -- a worker's liveness is "is the process still
# there," which ECS's own task lifecycle already gives it. The honest signal
# for whether it is doing anything is the ingest queue's depth (`make queues`,
# or the CloudWatch metric behind its Application Auto Scaling policy).
CMD ["python", "workers/ingest/main.py"]
