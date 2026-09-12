"""Deciding when a file, and then a session, is finished.

This lived inside the ingest worker until the GPU started reporting its own
results. Now two different processes need it — the worker, when a document
fails early, and the broker, when the GPU says a job is done — so it lives
here and both import it.

Everything here is synchronous. The broker is an async endpoint and calls it
through `run_in_threadpool`, which is the same discipline the rest of the API
follows for anything that blocks.
"""
import logging

from sqlalchemy import func, select, update

from . import events, models

log = logging.getLogger(__name__)


def finish_file(db, session_id: str) -> None:
    """Recount the session's files, and close it if none are outstanding.

    Counted, not incremented. An increment looks simpler and is wrong here:
    this can run on the failure path, immediately before a message goes back to
    the queue to be retried. A counter would then be bumped once per *attempt*
    rather than once per file, and with five attempts a one-file session would
    end up claiming six were done.

    Deriving the number from the files themselves is idempotent by
    construction, which is what at-least-once delivery requires.

    A file that failed and is about to be retried counts as settled for now, so
    a session can close as `failed` and then reopen as `ready` when the retry
    succeeds. That flap is deliberate: the alternative is a session that never
    closes at all while a doomed file exhausts its attempts.
    """
    db.rollback()                                # start clean after any failure

    settled = db.execute(
        select(func.count())
        .select_from(models.File)
        .where(models.File.session_id == session_id,
               models.File.status.in_((models.FILE_DONE, models.FILE_FAILED)))
    ).scalar_one()

    db.execute(
        update(models.Session)
        .where(models.Session.id == session_id)
        .values(files_done=settled)
    )
    db.commit()

    session = db.get(models.Session, session_id)
    db.refresh(session)                          # expire_on_commit=False keeps stale copies
    if settled < session.files_total:
        return

    failed = db.execute(
        select(func.count())
        .select_from(models.File)
        .where(models.File.session_id == session_id,
               models.File.status == models.FILE_FAILED)
    ).scalar_one()

    if failed == session.files_total:
        session.status = models.SESSION_FAILED
        session.error = "every file failed to process; see the per-file errors"
    else:
        session.status = models.SESSION_READY
        session.error = None
    db.commit()

    events.publish(session_id, {
        "event": events.SESSION_PROGRESS,
        "status": session.status,
        "files_done": session.files_done,
        "files_total": session.files_total,
        "error": session.error,
    })
    log.info("session %s -> %s", session_id, session.status)


def announce_file(session_id: str, row) -> None:
    events.publish(session_id, {
        "event": events.FILE_PROGRESS,
        "file_id": row.id,
        "filename": row.filename,
        "status": row.status,
        "chunk_count": row.chunk_count or 0,
        "error": row.error,
    })


def mark_failed(db, file_id: str, message: str) -> None:
    """Record why a file could not be processed, and settle its session.

    Refuses to downgrade a file that is already `done`. This exists because
    every caller (`workers/ingest/main.py::handle`) reaches this from one
    broad `except Exception` around an entire stage -- including, in v3, the
    "vectorize" stage, whose last two calls (`db.commit()` marking the file
    `done`, then `bookkeeping.finish_file`) can themselves still raise on a
    transient database error *after* the file has genuinely finished. Without
    this guard, that raises into the same `except` block, which calls this
    function and overwrites a correct `done` with `failed` -- the file was
    never actually broken, only the bookkeeping that ran one step after it
    finished was.
    """
    row = db.get(models.File, file_id)
    if row is None or row.status == models.FILE_DONE:
        return
    row.status = models.FILE_FAILED
    row.error = message[:2000]
    db.commit()
    announce_file(row.session_id, row)
    finish_file(db, row.session_id)
