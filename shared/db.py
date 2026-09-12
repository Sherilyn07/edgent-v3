"""Database connections — two of them, same reason as version 2.

The API is async and holds many mostly-idle connections, so it uses
`aiosqlite`/`asyncpg` and never blocks its event loop. The workers are
synchronous, because blocking is their whole job. One `DATABASE_URL`
configures both, and `_driver()` already normalises `postgres://` (which RDS
consoles still emit) to `postgresql://` so a URL copied from the AWS console
does not fail at import with an opaque "Can't load plugin".

What changed from version 2
----------------------------
**Schema creation moved out of the request path.** Version 2's `init_db()` ran
`create_all()` on every API and worker process startup, which is harmless
under SQLite (one file, one process really writing DDL at a time) and becomes
a real race the moment many EC2 instances and ECS tasks start concurrently
against shared RDS. In v3, schema changes are Alembic migrations
(`v3/migrations/`), run once as a one-off ECS task
(`python -m scripts.migrate`) -- never by the API or a worker. `init_db()` is
kept, but only as a local-SQLite convenience so `docker compose up` still
works without Alembic on a laptop; see `_is_sqlite()` below.

**pgvector needs one extra hook.** `pgvector.sqlalchemy.Vector` needs the
underlying psycopg2 connection to know how to encode/decode a Python list as
`vector` on the wire -- `pgvector.psycopg2.register_vector`. Only the sync
engine (used by the workers) ever reads or writes `Chunk.embedding`, so the
hook is registered there only, on every new connection, the same pattern
`_pragmas` already uses for SQLite below.
"""
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

log = logging.getLogger(__name__)
settings = get_settings()

BUSY_TIMEOUT_MS = 30_000


def _driver(url: str, driver: str | None) -> str:
    """Point a URL at a specific driver, keeping the rest of it intact."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    scheme, rest = url.split("://", 1)
    base = scheme.split("+", 1)[0]
    return f"{base}+{driver}://{rest}" if driver else f"{base}://{rest}"


def _is_sqlite() -> bool:
    return settings.database_url.startswith("sqlite")


def _is_postgres() -> bool:
    return settings.database_url.startswith(("postgres://", "postgresql://"))


def _pragmas(dbapi_connection, _record) -> None:
    """Applied to every new SQLite connection, in every process. Local dev only."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _register_pgvector(dbapi_connection, _record) -> None:
    """Teach psycopg2 how to bind a Python list of floats as `vector`.

    Without this, writing to `Chunk.embedding` from the sync engine (the only
    engine that ever does — see workers/ingest/main.py's "vectorize" stage and
    shared/vectorstore.py) fails or silently sends the wrong wire format.
    """
    from pgvector.psycopg2 import register_vector

    register_vector(dbapi_connection)


_pool_kwargs: dict = {} if _is_sqlite() else {
    "pool_size": settings.db_pool_size,
    "max_overflow": settings.db_max_overflow,
}


# --- async, for the API ------------------------------------------------------

_async_engine = create_async_engine(
    _driver(settings.database_url, "aiosqlite" if _is_sqlite() else "asyncpg"),
    pool_pre_ping=True,
    **_pool_kwargs,
)
AsyncSessionLocal = async_sessionmaker(_async_engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, always closed."""
    async with AsyncSessionLocal() as session:
        yield session


# --- sync, for the workers ---------------------------------------------------

_sync_engine = create_engine(
    _driver(settings.database_url, None if _is_sqlite() else "psycopg2"),
    pool_pre_ping=True,
    **_pool_kwargs,
)

if _is_sqlite():
    event.listen(_sync_engine, "connect", _pragmas)
    # The async engine wraps a sync one; the event goes on the inner engine.
    event.listen(_async_engine.sync_engine, "connect", _pragmas)

if _is_postgres():
    event.listen(_sync_engine, "connect", _register_pgvector)

SyncSessionLocal = sessionmaker(bind=_sync_engine, autoflush=False, expire_on_commit=False)


@contextmanager
def worker_session() -> Iterator[Session]:
    """One database session per message handled, always closed."""
    session = SyncSessionLocal()
    try:
        yield session
    finally:
        session.close()


# --- schema ------------------------------------------------------------------

def init_db() -> None:
    """Local SQLite convenience only. Postgres is migrated by Alembic instead.

    In AWS this is intentionally *not* called by the API or the workers — see
    `scripts/migrate.py` and `v3/migrations/`. Calling it against Postgres
    would race across every EC2 instance and ECS task starting at once, and it
    cannot apply the pgvector migration (extension + index) that a plain
    `create_all()` does not know how to do.
    """
    if not _is_sqlite():
        log.info("database_url is not sqlite; schema is managed by Alembic "
                 "(run `python -m scripts.migrate` once, not on every start-up)")
        return
    Base.metadata.create_all(bind=_sync_engine)
    with _sync_engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        log.info("sqlite journal_mode=%s — local dev only; pgvector needs Postgres", mode)
