"""The database tables.

Still four: `chunks` is still the one that changed most, and it changes again
in v3. In version 2, a chunk's text lived here and its vector lived in Chroma,
addressed by `chunk_key`. In v3, both live here.

    embedding = Column(Vector(384), nullable=True)

Nullable, and deliberately so: a chunk's text is written first, and its vector
arrives later once the GPU has embedded it (see workers/ingest/main.py's
"vectorize" stage). A chunk with no vector yet simply cannot be returned by a
similarity search -- `WHERE embedding IS NOT NULL` in vectorstore.py is what
that costs, and it is the same "text before vector, so a failure is harmless"
ordering version 2 already used when Chroma held the vector instead.

384 is `all-MiniLM-L6-v2`'s output size (services/embedding/config.py). If that
model ever changes, every existing vector is invalid and the column has to be
resized and re-populated -- vectors from two different models are not
comparable, the same rule version 2 already documented for Chroma.

`owner_id` is no longer a comment about the future. Cognito's `sub` claim goes
here now (see shared/auth.py and api/routes/sessions.py), and every
session-scoped route filters by it.
"""
import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --- status vocabulary ------------------------------------------------------
SESSION_CREATED = "created"
SESSION_PROCESSING = "processing"
SESSION_READY = "ready"
SESSION_FAILED = "failed"

FILE_PENDING = "pending"
FILE_PROCESSING = "processing"
FILE_DONE = "done"
FILE_FAILED = "failed"

MESSAGE_PENDING = "pending"
MESSAGE_ANSWERING = "answering"
MESSAGE_DONE = "done"
MESSAGE_FAILED = "failed"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

# The embedding model's output size (services/embedding/config.py:
# sentence-transformers/all-MiniLM-L6-v2). Re-index everything if this changes.
EMBEDDING_DIM = 384


class Session(Base):
    """One person's upload-and-chat session."""

    __tablename__ = "sessions"

    id = Column(String(36), primary_key=True, default=new_id)
    # The Cognito `sub` claim of whoever created this session. Set once, at
    # creation, and checked on every route that touches the session afterwards.
    owner_id = Column(String(128), nullable=True, index=True)

    status = Column(String(20), nullable=False, default=SESSION_CREATED)
    error = Column(Text, nullable=True)

    # How a session knows it is finished without a coordinator. After each
    # file settles, the worker recounts the settled files and stores the
    # total; whoever brings the two to equality marks the session ready.
    #
    # Recounted rather than incremented, deliberately. See finish_file in
    # bookkeeping.py -- an increment runs once per delivery attempt rather
    # than once per file, so a retried file would be counted twice.
    files_total = Column(Integer, nullable=False, default=0)
    files_done = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), default=now_utc, server_default=func.now(),
                        onupdate=func.now())


class File(Base):
    """One uploaded file, and how far through processing it is."""

    __tablename__ = "files"

    id = Column(String(36), primary_key=True, default=new_id)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"),
                        nullable=False, index=True)

    filename = Column(String(400), nullable=False)
    kind = Column(String(20), nullable=False)
    raw_key = Column(String(600), nullable=False)
    text_key = Column(String(600), nullable=True)

    status = Column(String(20), nullable=False, default=FILE_PENDING)
    chunk_count = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    size_bytes = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), default=now_utc, server_default=func.now(),
                        onupdate=func.now())


class Chunk(Base):
    """A passage of text, and now its vector too.

    `chunk_key` is `{file_id}:{ordinal:04d}` -- deterministic, so re-indexing a
    file updates rows rather than duplicating them (see the upsert in
    workers/ingest/main.py).
    """

    __tablename__ = "chunks"

    id = Column(String(36), primary_key=True, default=new_id)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"),
                        nullable=False)
    file_id = Column(String(36), ForeignKey("files.id", ondelete="CASCADE"), nullable=False)

    chunk_key = Column(String(80), nullable=False)

    source = Column(String(400), nullable=False)     # the original filename
    section = Column(String(400), nullable=True)     # a heading trail, or a timestamp range
    kind = Column(String(20), nullable=False)
    ordinal = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)

    # Written by the ingest worker's "vectorize" stage, once the embedding
    # service has processed this file's chunks.jsonl. NULL until then.
    embedding = Column(Vector(EMBEDDING_DIM), nullable=True)

    created_at = Column(DateTime(timezone=True), default=now_utc, server_default=func.now())

    __table_args__ = (
        Index("ix_chunks_session", "session_id"),
        Index("ix_chunks_file", "file_id"),
        # Retrieval looks chunks up by key within a session, and this is also
        # what makes re-indexing an upsert instead of a duplicate.
        UniqueConstraint("session_id", "chunk_key", name="uq_chunks_session_key"),
        # HNSW over IVFFlat: chunks are appended continuously as documents
        # finish indexing, so there is never a stable "build once over a
        # representative sample" moment IVFFlat wants. HNSW builds
        # incrementally and needs no such step. vector_cosine_ops matches
        # Chroma's "hnsw:space": "cosine" setting from version 2, so ranking
        # behaviour is unchanged by the move.
        Index(
            "ix_chunks_embedding", "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Message(Base):
    """One thing said -- by the user or by the model."""

    __tablename__ = "messages"

    id = Column(String(36), primary_key=True, default=new_id)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"),
                        nullable=False, index=True)

    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default=MESSAGE_PENDING)
    sources = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=now_utc, server_default=func.now())
