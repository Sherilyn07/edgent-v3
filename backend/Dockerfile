# The API image. Runs on EC2 inside docker-compose.ec2.yml, behind an ALB.
#
# Build from the v3 repo root so `shared/` and `backend/` are both in context
# -- this is what lets three separately-built images (this one,
# workers/ingest, workers/chat) share one copy of `shared/` without a
# published package:
#
#   docker build -f backend/Dockerfile -t edgentrag/api .

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# Dependencies first, as their own layer: this is the expensive step and it is
# only redone when a requirements file changes, not on every code edit.
COPY shared/requirements.txt /app/shared/requirements.txt
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r shared/requirements.txt -r backend/requirements.txt

COPY shared /app/shared
COPY backend /app/backend

# Bundled so a one-off migration task can run from this image too:
#   docker compose run --rm api python -m scripts.migrate
# or, in AWS, `aws ecs run-task` with this image and that command -- see
# DEPLOY.md's ECS section.
COPY scripts /app/scripts
COPY migrations /app/migrations

# Never run as root.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000

# Liveness only -- deliberately no database/Redis check. See shared's
# health.py docstring for why.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

# One uvicorn worker per container. Scale by running more EC2 instances (the
# Auto Scaling Group), not more workers inside one -- a crash then costs one
# instance rather than all of them.
CMD ["uvicorn", "backend.api.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
