# The chat worker's own image — deployed as its own ECS Fargate service.
# Deliberately smaller than ingest-worker's: no Docling, no native libraries.
#
# Build from the v3 repo root so `shared/` is in context:
#   docker build -f workers/chat/Dockerfile -t edgentrag/chat-worker .

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY shared/requirements.txt /app/shared/requirements.txt
COPY workers/chat/requirements.txt /app/workers/chat/requirements.txt
RUN pip install -r shared/requirements.txt -r workers/chat/requirements.txt

COPY shared /app/shared
COPY workers/chat /app/workers/chat

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

CMD ["python", "workers/chat/main.py"]
