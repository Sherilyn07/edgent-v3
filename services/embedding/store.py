"""The vector database.

STUDENT WORK — three functions.

Chroma is a vector database you can run with no server: point it at a folder
and it keeps everything there. One collection per session, so two users can
never see each other's documents.
"""
import logging

from .config import get_settings

log = logging.getLogger(__name__)
settings = get_settings()

_client = None


def get_client():
    """Open Chroma once. PROVIDED — working.

    Imported here rather than at the top of the file so the service still
    starts if Chroma is not installed yet. /health will work; the moment you
    call /embed you will get a clear ImportError instead of a service that
    refuses to boot.
    """
    global _client
    if _client is None:
        import chromadb

        log.info("opening chroma at %s", settings.chroma_dir)
        _client = chromadb.PersistentClient(path=settings.chroma_dir)
    return _client


def collection_name(session_id: str) -> str:
    """PROVIDED — working."""
    return f"session_{session_id.replace('-', '')}"


def get_collection(session_id: str):
    """Fetch or create this session's collection.

    What to write:

        return get_client().get_or_create_collection(
            name=collection_name(session_id),
            metadata={"hnsw:space": "cosine"},
        )

    The metadata sets the distance measure to cosine, which is what you want
    for normalised text embeddings. It is fixed when the collection is created
    and cannot be changed afterwards, so getting it right now matters.
    """
    return get_client().get_or_create_collection(
        name=collection_name(session_id),
        metadata={"hnsw:space": "cosine"},
    )


def add_chunks(session_id: str, chunks: list[dict], vectors: list[list[float]]) -> None:
    """Store chunks and their vectors.

    `chunks` are the records from the .jsonl files; `vectors` is what
    embed_texts returned, in the same order.

    What to write:

        collection = get_collection(session_id)
        collection.upsert(
            ids=[c["chunk_id"] for c in chunks],
            embeddings=vectors,
            documents=[c["text"] for c in chunks],
            metadatas=[{
                "chunks_key": c["chunks_key"],   # added by app.py before this call
                "source":     c.get("source", ""),
                "section":    c.get("section", ""),
            } for c in chunks],
        )

    Use `upsert`, not `add`. If the same session is indexed twice, upsert
    quietly replaces; `add` raises on a duplicate id.

    `chunks_key` in the metadata is the important one. It is how the monolith
    finds the chunk's text later -- the retrieve endpoint returns it rather
    than the text itself.
    """
    collection = get_collection(session_id)
    collection.upsert(
        ids=[c["chunk_id"] for c in chunks],
        embeddings=vectors,
        documents=[c["text"] for c in chunks],
        metadatas=[
            {
                "chunks_key": c["chunks_key"],
                "source": c.get("source", ""),
                "section": c.get("section", ""),
            }
            for c in chunks
        ],
    )


def search(session_id: str, vector: list[float], top_k: int) -> list[dict]:
    """Find the closest chunks to a query vector.

    What to write:

      1. collection = get_collection(session_id)
      2. result = collection.query(query_embeddings=[vector], n_results=top_k,
                                   include=["metadatas", "distances"])
      3. Chroma returns lists-of-lists because it supports several queries at
         once. We asked one question, so everything you want is at index [0]:
         result["ids"][0], result["distances"][0], result["metadatas"][0].
      4. Build one dictionary per hit:

             {"chunk_id", "chunks_key", "score", "source", "section"}

         Chroma gives you a *distance* -- smaller is better. People expect a
         *score* where bigger is better, so convert: score = 1 - distance.

      5. Return them best first.

    `get_or_create_collection` means the collection always exists, but it can
    be empty -- if nothing has been indexed yet, Chroma returns empty lists and
    your loop should simply produce no hits rather than raising.
    """
    collection = get_collection(session_id)
    result = collection.query(
        query_embeddings=[vector], n_results=top_k, include=["metadatas", "distances"]
    )

    hits = []
    for chunk_id, distance, metadata in zip(
        result["ids"][0], result["distances"][0], result["metadatas"][0]
    ):
        metadata = metadata or {}
        hits.append(
            {
                "chunk_id": chunk_id,
                "chunks_key": metadata.get("chunks_key", ""),
                "score": 1 - distance,
                "source": metadata.get("source", ""),
                "section": metadata.get("section", ""),
            }
        )

    hits.sort(key=lambda hit: hit["score"], reverse=True)
    return hits
