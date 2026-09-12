"""The API — thin, async, and stateless. Runs on EC2, behind an ALB, scaled by
an Auto Scaling Group — see DEPLOY.md.

Same rule as version 2: it validates a request, writes a row, puts a message on
a queue, and returns. Nothing here waits for a model, opens a file, or does
anything measured in seconds. Every handler is `async def`.

What changed from version 2
----------------------------
**No `init_db()` at startup.** Version 2 ran `create_all()` on every process
start, safe under one SQLite file and a real race once many EC2 instances
start concurrently against shared RDS. Schema is now Alembic's job, run once,
separately — see `scripts/migrate.py` and `shared/db.py`'s docstring.

**Imports are absolute, not relative.** `shared` is a top-level package inside
this image now (`COPY shared /app/shared` in the Dockerfile), not nested under
`backend` — the same package three separately-built images all copy in
unmodified, so `backend`, `workers/ingest` and `workers/chat` can be deployed
independently without duplicating this code.

**Two new routers.** `tickets` mints the short-lived SSE ticket Cognito auth
requires (see routes/tickets.py); the rest of the routers gained an auth
dependency each but did not change shape.

    uvicorn backend.api.main:app --host 0.0.0.0 --port 8000
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from shared.auth import cognito_configured
from shared.config import get_settings
from .routes import broker, chat, config, events, health, sessions, tickets, uploads

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("api")

app = FastAPI(title="EdgentRAG v3 — API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(config.router)
app.include_router(sessions.router)
app.include_router(uploads.router)
app.include_router(chat.router)
app.include_router(events.router)
app.include_router(tickets.router)
# The GPU's only way in. See routes/broker.py for why it exists, and why its
# bearer token is a separate credential system from Cognito.
app.include_router(broker.router)


@app.on_event("startup")
async def on_startup() -> None:
    log.info("env          : %s", settings.env)
    log.info("database     : %s", settings.database_url.split("@")[-1])
    log.info("redis        : %s", settings.redis_url)
    log.info("bucket       : %s", settings.s3_bucket)
    for name in ("ingest", "chat", "stt", "embed"):
        url = getattr(settings, f"{name}_queue_url", "")
        log.info("%-9s queue: %s", name, url.rsplit("/", 1)[-1] if url else "(unset)")
    log.info("broker       : %s", "configured" if settings.broker_token else "NO TOKEN SET")
    log.info("cognito      : %s", "configured" if cognito_configured() else "NOT CONFIGURED (auth is off)")
