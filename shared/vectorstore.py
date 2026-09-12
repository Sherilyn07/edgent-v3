"""pgvector: the query the chat worker runs, and the bulk write the ingest
worker runs once the GPU has embedded a file's chunks.

New in v3. Version 2 had no equivalent — retrieval was a call to the embedding
service's `/retrieve`, which searched Chroma and returned keys and scores; the
text came from a second, separate database query. Here both are one query,
because the vector and the text now live in the same row.

Both functions go through SQLAlchemy's expression language rather than hand
written SQL strings, specifically so a plain Python `list[float]` gets
serialized correctly: `pgvector.sqlalchemy.Vector`'s bind processor is what
turns it into the wire format Postgres expects, and that processor only runs
when a value is bound through a column (or a `bindparam` typed as `Vector`) —
not through an untyped parameter in raw SQL text.
"""
import logging

from sqlalchemy import bindparam, select, update
from sqlalchemy.orm import Session

from . import models

log = logging.getLogger(__name__)

# Batched, not one UPDATE per row: a document can produce hundreds of chunks,
# and one prepared statement executed with a list of parameter sets is far
# cheaper than that many round trips. 500 keeps a batch well under any
# reasonable statement/packet size limit while still being a handful of round
# trips even for a large document.
UPDATE_BATCH_SIZE = 500


def bulk_set_embeddings(db: Session, session_id: str, vectors: list[dict]) -> int:
    """Write vectors into already-stored chunk rows.

    `vectors` is `[{"chunk_id": ..., "embedding": [float, ...]}, ...]` — the
    same shape the embedding service streams out to `vectors_url` (see
    services/embedding/jobs.py). `chunk_id` there is the chunk_key this
    session's rows are keyed on, not a database primary key.

    Idempotent by construction: this is a plain UPDATE keyed on
    (session_id, chunk_key), so a redelivered "vectorize" message just writes
    the same values again.
    """
    # Against the Core Table, not the ORM entity (`update(models.Chunk)`):
    # SQLAlchemy 2.0 treats an executemany-style list of parameter dicts
    # against an ORM-mapped UPDATE as "bulk ORM update by primary key" and
    # demands a primary key in every dict, which is not the shape of this
    # update at all -- it is keyed on (session_id, chunk_key), by design,
    # since the embedding service only ever knows a chunk by that key, not
    # its database id. The Core table sidesteps that special-casing entirely
    # and executes exactly the plain, criteria-based UPDATE this is.
    table = models.Chunk.__table__
    stmt = (
        update(table)
        .where(
            table.c.session_id == bindparam("b_session_id"),
            table.c.chunk_key == bindparam("b_chunk_key"),
        )
        .values(embedding=bindparam("b_embedding", type_=table.c.embedding.type))
    )

    updated = 0
    for start in range(0, len(vectors), UPDATE_BATCH_SIZE):
        batch = vectors[start:start + UPDATE_BATCH_SIZE]
        db.execute(stmt, [
            {
                "b_session_id": session_id,
                "b_chunk_key": v["chunk_id"],
                "b_embedding": v["embedding"],
            }
            for v in batch
        ])
        db.commit()
        updated += len(batch)
    return updated


def search(db: Session, session_id: str, vector: list[float], top_k: int) -> list[dict]:
    """The closest chunks to a query vector, best first.

    `embedding.is_not(None)` is what replaces version 2's "chunk is indexed
    but not stored" orphan check -- a chunk that has not been vectorized yet
    simply cannot match, rather than matching and then failing to resolve.

    `cosine_distance` compiles to pgvector's `<=>` operator; smaller is
    better, so it is converted to a score (bigger is better) the same way
    version 2's Chroma wrapper did: `score = 1 - distance`.
    """
    distance = models.Chunk.embedding.cosine_distance(vector).label("distance")
    rows = db.execute(
        select(models.Chunk.chunk_key, models.Chunk.source, models.Chunk.section,
              models.Chunk.text, distance)
        .where(models.Chunk.session_id == session_id, models.Chunk.embedding.is_not(None))
        .order_by(distance)
        .limit(top_k)
    ).all()

    return [
        {
            "chunk_id": row.chunk_key,
            "source": row.source,
            "section": row.section,
            "text": row.text,
            "score": 1 - row.distance,
        }
        for row in rows
    ]
