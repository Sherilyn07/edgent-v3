"""Indexing as a pulled job. Retrieval stays a call — mostly.

Same split as version 2:

    indexing    bulk, slow, nobody waiting     -> queued, pulled through the broker
    retrieval   one question, a person waiting -> a direct call

What changed in v3
-------------------
The backend's Postgres now holds the vectors, not just Chroma. So this job
does one more thing after each batch: alongside the existing
`store.add_chunks(...)` write (kept for now — see the module docstring in
`store.py`), it accumulates `{chunk_id, embedding}` for every chunk it
embedded, and once the whole file is done, PUTs that list to
`body["vectors_url"]` — a presigned link the ingest worker gave it, the same
pattern this file already uses for the chunks it *reads*, just in reverse.

Nothing about the model, the batching, or Chroma changed. This is the smallest
change that gets the vectors to the backend without touching how they are
computed.
"""
import logging

import broker
from broker import iter_jsonl, upload_json
from . import model, store
from .config import get_settings

log = logging.getLogger("embedding.jobs")
settings = get_settings()

JOB = "embed"


def handle(body: dict) -> dict:
    """Index every chunk in the file this job points at.

    The chunks arrive as a `.jsonl` file behind a signed link rather than
    inside the message, because SQS caps a message at 256 KB and a real
    document is far past that. We stream it, so the size of the document does
    not decide the size of this process.

    Idempotent: Chroma is given deterministic ids, so re-running a redelivered
    job overwrites the same vectors rather than adding duplicates. The
    vectors written to `vectors_url` are equally safe to redeliver — the
    ingest worker's "vectorize" stage on the other end is a plain UPDATE
    keyed on (session_id, chunk_key), which a repeat just overwrites too.
    """
    session_id = body["session_id"]
    model.load_model()                      # a no-op after the first job

    indexed = 0
    computed: list[dict] = []                # {"chunk_id", "embedding"} for vectors_url
    batch: list[dict] = []
    for record in iter_jsonl(body["chunks_url"]):
        batch.append(record)
        if len(batch) >= settings.batch_size:
            indexed += _index(session_id, batch, computed)
            batch = []
    if batch:
        indexed += _index(session_id, batch, computed)

    vectors_url = body.get("vectors_url")
    if vectors_url and computed:
        # One JSON document -- a list of {"chunk_id", "embedding"} -- written
        # with the same one-shot PUT helper the STT service uses for its
        # transcript. The ingest worker reads it back with a single
        # json.loads(storage.get_text(...)), not line by line. See
        # workers/ingest/main.py's "vectorize" stage.
        upload_json(vectors_url, computed)
        log.info("wrote %d vectors to %s", len(computed), body.get("filename", session_id))

    log.info("indexed %d chunks for %s", indexed, body.get("filename", session_id))
    return {"indexed": indexed}


def _index(session_id: str, batch: list[dict], computed: list[dict]) -> int:
    vectors = model.embed_texts([c["text"] for c in batch])
    store.add_chunks(session_id, batch, vectors)
    computed.extend(
        {"chunk_id": c["chunk_id"], "embedding": vector}
        for c, vector in zip(batch, vectors)
    )
    return len(batch)


def poller(client: broker.BrokerClient) -> broker.JobPoller:
    return broker.JobPoller(client, JOB, handle, name="embed-jobs")
