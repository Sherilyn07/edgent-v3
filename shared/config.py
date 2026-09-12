"""Every setting, for all three programs (the API, the ingest worker, the chat worker).

Unchanged in shape from version 2 — one settings object, one `.env` — with the
additions version 3 needs: Postgres pooling was already here waiting to be used,
and Cognito is new.

There is still no "local vs cloud" switch. `DATABASE_URL` and `REDIS_URL` point
at SQLite/local-Redis for a laptop and at RDS/ElastiCache in AWS; nothing else in
the codebase has to know which.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "local"                      # "local" or "aws"
    log_level: str = "INFO"

    # --- database -------------------------------------------------------------
    # SQLite locally (WAL mode — see db.py); RDS Postgres + pgvector in AWS.
    # Point this at Postgres and nothing else changes except running the Alembic
    # migrations once — that is the whole reason it is a URL.
    database_url: str = "sqlite:////data/edgentrag.db"
    db_pool_size: int = 10
    db_max_overflow: int = 5

    # --- redis ------------------------------------------------------------------
    # Two jobs: the conversation window, and the pub/sub channel that carries
    # progress and tokens from a worker to whichever API task holds the browser's
    # connection. ElastiCache in AWS — see events.py for the non-cluster-mode
    # requirement that comes with pub/sub.
    redis_url: str = "redis://localhost:6379/0"
    history_turns: int = 6

    # --- object storage -----------------------------------------------------
    s3_bucket: str = "edgentrag-local"
    s3_endpoint_url: str | None = None      # None for real S3; set for an S3-compatible store
    aws_region: str = "us-east-1"
    presign_expiry_seconds: int = 3600
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024      # 2 GiB

    # --- queues -------------------------------------------------------------
    sqs_endpoint_url: str | None = None     # None for real SQS; set only to point elsewhere
    ingest_queue_url: str = ""
    chat_queue_url: str = ""
    # The two queues the GPU drains through the broker. It never sees these
    # URLs -- only the broker does.
    stt_queue_url: str = ""
    embed_queue_url: str = ""
    queue_wait_seconds: int = 20            # long polling: one call, twenty seconds
    queue_visibility_seconds: int = 900     # how long a worker owns a message
    queue_batch_size: int = 5
    # How many deliveries before SQS sets a message aside in the dead-letter
    # queue. bootstrap.py configures the queue with this; the broker uses it to
    # recognise a last attempt and give up cleanly. One value, two readers.
    queue_max_receives: int = 5

    # --- the three model services (Colab, unchanged in spirit from version 1) --
    embedding_service_url: str = "http://localhost:8001"
    stt_service_url: str = "http://localhost:8002"
    llm_service_url: str = "http://localhost:8003"
    service_timeout_seconds: int = 900
    stt_timeout_seconds: int = 3600
    service_retries: int = 3
    service_backoff_seconds: float = 1.5

    # --- retrieval and generation -------------------------------------------
    chunk_words: int = 200
    chunk_overlap_words: int = 40
    embed_batch_size: int = 256
    top_k: int = 4
    max_new_tokens: int = 400
    temperature: float = 0.3

    # --- the broker ----------------------------------------------------------
    # One shared token, which is all the GPU ever holds. It grants "ask for a
    # job" and nothing else -- no bucket, no queue, no other session. Revoking
    # it is a line in .env. Generate one with:  openssl rand -hex 32
    #
    # This is a *separate* credential system from Cognito below: the broker
    # token authenticates a machine (the Colab GPU) that can never hold an AWS
    # or Cognito identity; Cognito authenticates a person using the app.
    broker_token: str = ""
    job_url_expiry_seconds: int = 6 * 3600

    # --- cognito (new in v3) --------------------------------------------------
    # Verifying a user's ID token needs the pool's issuer/audience and its JWKS,
    # fetched from `https://cognito-idp.{region}.amazonaws.com/{user_pool_id}`.
    # See shared/auth.py. Leaving these unset disables auth entirely, which is
    # useful for local development against SQLite before a User Pool exists.
    cognito_region: str = ""
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""
    # A short-lived ticket, not the JWT itself, is what an SSE connection
    # carries -- see routes/tickets.py and routes/events.py for why.
    sse_ticket_seconds: int = 300

    # --- api ----------------------------------------------------------------
    cors_origins: str = "*"
    sse_heartbeat_seconds: int = 15         # keeps intermediaries from closing the stream


@lru_cache
def get_settings() -> Settings:
    return Settings()
