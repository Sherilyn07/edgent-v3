"""The ingest worker: prepare work for the GPU, and store what comes back.

A fully separate ECS Fargate service in v3 — not a `command:` variant of a
shared worker image the way version 2 built it, mirroring how the AI
`services/` are already separate from the backend. This is the only worker
that carries Docling (see requirements.txt / Dockerfile), because it is the
only one that ever converts a document.

Its state machine gained one stage since version 2:

    stage "start"        a video     -> put an stt job on the queue, stop
                         a document  -> convert, chunk, store, queue an embed job, stop
    stage "transcribed"  (the broker sends this when the GPU finishes)
                                     -> chunk the transcript, store, queue an embed job, stop
    stage "vectorize"    (the broker sends this when an embed job finishes)
                                     -> read the vectors the GPU wrote, bulk-UPDATE
                                        Chunk.embedding, mark the file done, stop

That last stage is new. In version 2 the GPU wrote vectors straight into its
own Chroma store and the file was done the moment the broker heard "embed
succeeded." In v3 the vectors have to land in our own Postgres
(`Chunk.embedding`, pgvector), and writing potentially hundreds of rows is
bulk work — it belongs here, in a worker whose death costs nothing, not
synchronously inside the API's threadpool. See shared/vectorstore.py and
api/routes/broker.py's docstring.

Every arrow still ends in "stop." No step waits for the next one.

    python workers/ingest/main.py
"""
import json
import logging
import os
import tempfile

from sqlalchemy.dialects.postgresql import insert

from shared import bookkeeping, convert, models, queues, storage, vectorstore
from shared.chunking import batched, iter_file_chunks
from shared.config import get_settings
from shared.db import init_db, worker_session
from shared.worker import Worker

log = logging.getLogger(__name__)
settings = get_settings()


# --- the job -----------------------------------------------------------------

def handle(body: dict) -> None:
    session_id = body["session_id"]
    file_id = body["file_id"]
    stage = body.get("stage", "start")

    with worker_session() as db:
        row = db.get(models.File, file_id)
        if row is None:
            log.warning("no such file %s; nothing to do", file_id)
            return
        if row.status == models.FILE_DONE:
            # A redelivery of something already finished. Acknowledge and move on.
            log.info("%s is already done; skipping", row.filename)
            return

        row.status = models.FILE_PROCESSING
        row.error = None
        db.commit()
        bookkeeping.announce_file(session_id, row)

        try:
            if stage == "vectorize":
                _vectorize(db, row)
            elif stage == "transcribed":
                _chunk_transcript(db, row)
            elif row.kind == convert.KIND_VIDEO:
                _request_transcription(row)
            else:
                _prepare_document(db, row)
        except Exception as exc:                 # noqa: BLE001
            db.rollback()
            bookkeeping.mark_failed(db, file_id, str(exc))
            raise                                 # let the queue retry it


# --- stage: a video needs the GPU before anything else can happen -------------

def _request_transcription(row) -> None:
    """Put an stt job on the queue and stop.

    Two signed URLs go in the message and nothing else of substance: one that
    lets the GPU read the video out of the bucket, one that lets it write the
    transcript back.
    """
    expiry = settings.job_url_expiry_seconds
    queues.send(settings.stt_queue_url, {
        "session_id": row.session_id,
        "file_id": row.id,
        "filename": row.filename,
        "media_url": storage.presign_get(row.raw_key, expiry),
        "result_url": storage.presign_put(
            storage.transcript_key(row.session_id, row.id),
            "application/json", expiry),
    })
    log.info("%s: queued for transcription", row.filename)


def _chunk_transcript(db, row) -> None:
    """The GPU has written a transcript. Turn it into chunks and index them."""
    payload = json.loads(storage.get_text(storage.transcript_key(row.session_id, row.id)))

    storage.put_text(storage.text_key(row.session_id, row.id),
                     payload.get("transcript", ""))
    row.text_key = storage.text_key(row.session_id, row.id)
    db.commit()

    chunks = [
        {
            "chunk_key": c.get("chunk_id") or f"{row.id}:{i:04d}",
            "session_id": row.session_id,
            "file_id": row.id,
            "source": row.filename,
            "kind": "video",
            "section": c.get("section"),
            "ordinal": c.get("order", i),
            "text": c.get("text", ""),
        }
        for i, c in enumerate(payload.get("chunks", []))
    ]
    _store_and_queue(db, row, iter(chunks))


# --- stage: a document can be prepared without the GPU ------------------------

def _prepare_document(db, row) -> None:
    """Download, convert, chunk, store — all through files on disk."""
    with tempfile.TemporaryDirectory() as tmp:
        raw_path = os.path.join(tmp, os.path.basename(row.raw_key) or "upload")
        text_path = os.path.join(tmp, "extracted.txt")

        storage.download_to(row.raw_key, raw_path)
        convert.to_text_file(raw_path, row.kind, text_path)

        key = storage.text_key(row.session_id, row.id)
        storage.upload_file(key, text_path)
        row.text_key = key
        db.commit()

        chunks = iter_file_chunks(
            text_path,
            session_id=row.session_id, file_id=row.id,
            source=row.filename, kind=row.kind,
        )
        _store_and_queue(db, row, chunks)


# --- storing, and handing indexing to the GPU --------------------------------

def _store_and_queue(db, row, chunks) -> None:
    """Write the chunks to the database and to storage, then queue one embed job.

    The database first, deliberately. If indexing never happens, the text is
    still stored and the upsert makes a second attempt harmless. The reverse
    order could leave a vector pointing at text that was never written.

    The chunks also go to storage as a `.jsonl` file, because that is what the
    GPU will read. They cannot travel in the message itself: SQS caps a
    message at 256 KB.
    """
    total = 0
    chunks_path = None
    try:
        handle_, chunks_path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(handle_, "w", encoding="utf-8") as out:
            for batch in batched(chunks, settings.embed_batch_size):
                db.execute(
                    insert(models.Chunk)
                    .values([
                        {
                            "id": models.new_id(),
                            "session_id": c["session_id"],
                            "file_id": c["file_id"],
                            "chunk_key": c["chunk_key"],
                            "source": c["source"],
                            "section": c.get("section"),
                            "kind": c["kind"],
                            "ordinal": c["ordinal"],
                            "text": c["text"],
                        }
                        for c in batch
                    ])
                    .on_conflict_do_update(
                        # By column, not by constraint name: naming the
                        # columns is portable across dialects, which is how
                        # this line survived the move from SQLite to Postgres
                        # unchanged in version 3.
                        index_elements=["session_id", "chunk_key"],
                        set_={"text": insert(models.Chunk).excluded.text,
                              "section": insert(models.Chunk).excluded.section,
                              "ordinal": insert(models.Chunk).excluded.ordinal},
                    )
                )
                db.commit()

                for c in batch:
                    out.write(json.dumps({
                        "chunk_id": c["chunk_key"],
                        "text": c["text"],
                        "source": c.get("source") or "",
                        "section": c.get("section") or "",
                        "chunks_key": "",
                    }) + "\n")
                total += len(batch)

        row.chunk_count = total
        db.commit()

        if total == 0:
            log.warning("%s produced no chunks; nothing to index", row.filename)
            row.status = models.FILE_DONE
            db.commit()
            bookkeeping.announce_file(row.session_id, row)
            bookkeeping.finish_file(db, row.session_id)
            return

        chunks_key = storage.chunks_key(row.session_id, row.id)
        storage.upload_file(chunks_key, chunks_path, "application/x-ndjson")
    finally:
        if chunks_path and os.path.exists(chunks_path):
            os.unlink(chunks_path)

    # New in v3: alongside the chunks link, a presigned PUT the GPU writes its
    # computed vectors to. See services/embedding/jobs.py.
    vectors_key = storage.vectors_key(row.session_id, row.id)
    queues.send(settings.embed_queue_url, {
        "session_id": row.session_id,
        "file_id": row.id,
        "filename": row.filename,
        "count": total,
        "chunks_url": storage.presign_get(chunks_key, settings.job_url_expiry_seconds),
        "vectors_url": storage.presign_put(vectors_key, "application/json",
                                           settings.job_url_expiry_seconds),
    })
    log.info("%s: %d chunks stored, queued for indexing", row.filename, total)


# --- stage: the GPU has embedded this file's chunks ---------------------------

def _vectorize(db, row) -> None:
    """Read the vectors the GPU wrote, bulk-UPDATE Chunk.embedding, finish.

    New in v3, and it is what `_advance("embed")` in api/routes/broker.py now
    schedules instead of marking the file done itself — writing potentially
    hundreds of rows is bulk work, and it belongs in a worker.

    Server-side S3 read via the task's own IAM role: no presign needed here,
    unlike every other cross-machine hop in this system, because this call
    never leaves our own account.
    """
    vectors_key = storage.vectors_key(row.session_id, row.id)
    records = json.loads(storage.get_text(vectors_key))

    updated = vectorstore.bulk_set_embeddings(db, row.session_id, records)
    log.info("%s: %d chunk vectors written to postgres", row.filename, updated)

    row.status = models.FILE_DONE
    row.chunk_count = row.chunk_count or updated
    row.error = None
    db.commit()
    bookkeeping.announce_file(row.session_id, row)
    bookkeeping.finish_file(db, row.session_id)


def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    init_db()
    Worker("ingest", settings.ingest_queue_url, handle).run()


if __name__ == "__main__":
    main()
