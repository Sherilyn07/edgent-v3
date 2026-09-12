"""The broker — how the GPU gets work without holding AWS credentials.

Unchanged in mechanism from version 2: three endpoints, a single shared bearer
token, and presigned URLs inside every job so the GPU never learns the
bucket's name. This is a *separate* credential system from Cognito
(shared/auth.py) — deliberately so. The broker token authenticates a machine
that can never hold an AWS or Cognito identity and grants exactly one thing,
"ask for a job." Cognito authenticates a person and grants "see your own
sessions." Do not merge them.

    POST /broker/claim       "have you got work?"   -> a job, or 204
    POST /broker/heartbeat   "still going"          -> keeps the job ours
    POST /broker/complete    "done" or "failed"     -> acknowledge, or let it retry

What changed from version 2
----------------------------
`_advance("embed")` no longer marks the file `done` itself. In v2 the vectors
lived in Chroma, written by the GPU directly — once the embed job reported
success there was nothing left to do. In v3 the vectors need to land in our
own Postgres (`Chunk.embedding`), and writing them is bulk work that belongs
in a worker, not in the API's threadpool — the one thing this whole design
forbids the API from doing. So a successful embed job now enqueues one more
`ingest` message, `stage: "vectorize"`, and it is `workers/ingest/main.py`
that reads the vectors the GPU wrote to S3 and bulk-UPDATEs the rows, then
marks the file done. See shared/vectorstore.py.
"""
import base64
import binascii
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from starlette.concurrency import run_in_threadpool

from shared import bookkeeping, events, models, queues
from shared.config import get_settings
from shared.db import worker_session
from shared.schemas import ClaimRequest, JobComplete, JobLease, JobOffer

router = APIRouter(prefix="/broker", tags=["broker"])
log = logging.getLogger(__name__)
settings = get_settings()

# Which queue each job name maps to. A name the GPU sends that is not in here
# is rejected, so a typo cannot make it poll something it should not.
QUEUES = {
    "stt": lambda: settings.stt_queue_url,
    "embed": lambda: settings.embed_queue_url,
}

# The same value bootstrap.py put on the queue, so a last attempt is
# recognisable as the last one.
MAX_RECEIVES = settings.queue_max_receives


# --- authentication ----------------------------------------------------------

def require_token(authorization: str = Header(default="")) -> None:
    """One shared token, sent as a bearer header.

    Not sophisticated, and deliberately so: the point of the pattern is that
    this credential is worth almost nothing. It authorises "ask for a job on
    one of two queues" and nothing else.
    """
    if not settings.broker_token:
        raise HTTPException(status_code=503,
                            detail="broker is not configured; set BROKER_TOKEN")
    expected = f"Bearer {settings.broker_token}"
    import hmac
    if not hmac.compare_digest(authorization.strip(), expected):
        raise HTTPException(status_code=401, detail="bad or missing broker token")


# --- leases ------------------------------------------------------------------

def _pack(queue: str, receipt_handle: str, attempt: int) -> str:
    raw = json.dumps({"q": queue, "rh": receipt_handle, "n": attempt}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _unpack(lease: str) -> queues.Message:
    try:
        data = json.loads(base64.urlsafe_b64decode(lease.encode()))
        queue_url = QUEUES[data["q"]]()
    except (KeyError, ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="unusable lease") from exc
    if not queue_url:
        # Mirrors claim()'s own check. A lease can outlive a config change --
        # heartbeat/complete called minutes after claim, against a queue that
        # was unset in between -- and without this, extend()/delete() would
        # fail on an empty queue_url with a raw boto3 error instead of this
        # same clear 503.
        raise HTTPException(status_code=503, detail=f"{data['q']} queue is not configured")
    return queues.Message(body={}, receipt_handle=data["rh"],
                          receive_count=int(data.get("n", 1)), queue_url=queue_url)


def _backoff(attempt: int) -> int:
    """How long to hide a failed message before offering it again.

    Zero would be wrong: a job that fails in two seconds would be reclaimed
    instantly, fail again, and burn all five attempts in about ten seconds.
    So: thirty seconds, doubling, capped at the visibility timeout.
    """
    return min(30 * (2 ** max(0, attempt - 1)), settings.queue_visibility_seconds)


# --- the three endpoints -----------------------------------------------------

@router.post("/claim", response_model=JobOffer | None,
             dependencies=[Depends(require_token)])
async def claim(body: ClaimRequest, response: Response):
    """Hand out one job, or answer 204 if there is nothing to do."""
    if body.job not in QUEUES:
        raise HTTPException(status_code=400, detail=f"unknown job type {body.job!r}")
    queue_url = QUEUES[body.job]()
    if not queue_url:
        raise HTTPException(status_code=503, detail=f"{body.job} queue is not configured")

    messages = await run_in_threadpool(queues.receive, queue_url, 1)
    if not messages:
        response.status_code = 204
        return None

    message = messages[0]
    log.info("handed a %s job to the gpu (attempt %d)", body.job, message.receive_count)
    return JobOffer(
        lease=_pack(body.job, message.receipt_handle, message.receive_count),
        job=body.job,
        attempt=message.receive_count,
        body=message.body,
    )


@router.post("/heartbeat", status_code=204, dependencies=[Depends(require_token)])
async def heartbeat(body: JobLease) -> Response:
    """Keep a long job from being handed to somebody else."""
    message = _unpack(body.lease)
    await run_in_threadpool(queues.extend, message, settings.queue_visibility_seconds)
    return Response(status_code=204)


@router.post("/complete", status_code=204, dependencies=[Depends(require_token)])
async def complete(body: JobComplete) -> Response:
    """The GPU reports the outcome.

    On success we acknowledge the message and move the file to its next
    stage. On failure we do *not* acknowledge: SQS makes the message visible
    again and the dead-letter queue catches it if it keeps failing.
    """
    message = _unpack(body.lease)

    if not body.ok:
        attempt = message.receive_count
        last = attempt >= MAX_RECEIVES
        log.warning("gpu reported failure on %s (attempt %d/%d): %s",
                    body.job, attempt, MAX_RECEIVES, body.error)

        if last:
            # Out of attempts. If the broker also stopped caring here, the
            # file would sit at "processing" for ever and its session would
            # never close -- the browser would spin with no error. So settle
            # it now.
            await run_in_threadpool(_give_up, body)
            await run_in_threadpool(queues.delete, message)
            return Response(status_code=204)

        await run_in_threadpool(_record_failure, body)
        await run_in_threadpool(queues.extend, message, _backoff(attempt))
        return Response(status_code=204)

    await run_in_threadpool(_advance, body)
    await run_in_threadpool(queues.delete, message)
    return Response(status_code=204)


# --- what a completed job means ----------------------------------------------

def _advance(body: JobComplete) -> None:
    """Move the file to whatever comes after the job that just finished."""
    if body.job == "stt":
        # The transcript is in the bucket. Put the file back on the ingest
        # queue so a worker -- not the GPU -- does the chunking and storing.
        queues.send(settings.ingest_queue_url, {
            "session_id": body.session_id,
            "file_id": body.file_id,
            "stage": "transcribed",
        })
        log.info("transcription done for %s; queued for chunking", body.file_id)
        return

    if body.job == "embed":
        # The GPU has embedded this file's chunks and written them to
        # `vectors_key(...)` in S3 (services/embedding/jobs.py). Writing them
        # into Postgres is a bulk database operation -- it belongs in a
        # worker, not synchronously inside this API threadpool call. So:
        # queue one more short step and let the ingest worker's "vectorize"
        # stage do the write and settle the file. Nothing here marks the file
        # done any more.
        queues.send(settings.ingest_queue_url, {
            "session_id": body.session_id,
            "file_id": body.file_id,
            "stage": "vectorize",
        })
        log.info("embedding done for %s; queued for vectorizing", body.file_id)


def _give_up(body: JobComplete) -> None:
    """The last attempt failed. Mark the file failed and settle the session."""
    with worker_session() as db:
        bookkeeping.mark_failed(
            db, body.file_id,
            f"{body.job} failed after {MAX_RECEIVES} attempts: {body.error}")
    log.error("%s gave up on file %s", body.job, body.file_id)


def _record_failure(body: JobComplete) -> None:
    """Show the failure while it is still being retried."""
    with worker_session() as db:
        row = db.get(models.File, body.file_id)
        if row is None:
            return
        events.publish(row.session_id, {
            "event": events.FILE_PROGRESS,
            "file_id": row.id,
            "filename": row.filename,
            "status": row.status,
            "chunk_count": row.chunk_count or 0,
            "error": f"{body.job} attempt failed: {body.error}",
        })
