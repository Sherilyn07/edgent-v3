"""pgvector: the extension, the column, the index.

The step that makes v3's chunk store also a vector store. RDS grants
`CREATE EXTENSION vector` to the master user by default — if this fails with
a permissions error, the instance is either not RDS or is pinned to an engine
version older than the one pgvector shipped in; see DEPLOY.md's RDS section.

HNSW, not IVFFlat: chunks are appended continuously per session as documents
finish indexing, so there is never a stable "build once over a representative
sample" moment IVFFlat wants. HNSW builds incrementally and needs no such
step. `vector_cosine_ops` matches Chroma's `"hnsw:space": "cosine"` setting
from version 2, so ranking behaviour is unchanged by the move.

Revision ID: 0002_pgvector
Revises: 0001_initial_schema
Create Date: 2026-09-11
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0002_pgvector"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 384      # sentence-transformers/all-MiniLM-L6-v2's output size


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("chunks", sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True))
    op.create_index(
        "ix_chunks_embedding", "chunks", ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_embedding", table_name="chunks")
    op.drop_column("chunks", "embedding")
    # Deliberately not dropping the extension: another table or a future
    # migration may depend on it, and CREATE EXTENSION IF NOT EXISTS makes
    # re-running upgrade() harmless either way.
