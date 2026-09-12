"""The speech-to-text service — port 8002.

One job: a video goes in, chunk records come out, in exactly the same format
the monolith produces for PDFs.

The media reaches us one of two ways, and which one depends on where the
monolith keeps its files:

    url   the monolith sends a presigned link and we fetch it ourselves.
          This is the S3 case, and the good one: the video travels from the
          bucket to here, once, and never occupies the monolith's memory.

    file  the monolith uploads the bytes to us. This is the local-disk case,
          where there is no address we could fetch from -- the files are on
          somebody's laptop.

Either way we stream to a temporary file. A lecture recording is far too big to
hold as one bytes object, on any machine.

Run it from the `backend/services` folder:

    uvicorn stt.app:app --port 8002
"""
import logging
import os
import shutil
import tempfile

import requests
from fastapi import APIRouter, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

import broker

from . import jobs, transcribe
from .config import get_settings, resolve_compute_type, resolve_device
from .schemas import TranscribeResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("stt")
settings = get_settings()

app = FastAPI(title="EdgentRAG v1 — Speech to Text Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter()

DOWNLOAD_CHUNK = 1024 * 1024      # 1 MB at a time


@router.get("/health")
def health() -> dict:
    # The resolved device, not the literal "auto" -- this is how you confirm
    # from outside that the GPU is actually being used.
    device = resolve_device(settings.device)
    return {"status": "ok", "service": "stt", "model": settings.whisper_model,
            "device": device,
            "compute_type": resolve_compute_type(settings.compute_type, device)}


def _fetch_to(url: str, local_path: str) -> int:
    """Stream a presigned URL onto disk. Returns the size in bytes."""
    with requests.get(url, stream=True, timeout=settings.download_timeout_seconds) as response:
        response.raise_for_status()
        with open(local_path, "wb") as handle:
            for block in response.iter_content(chunk_size=DOWNLOAD_CHUNK):
                if block:
                    handle.write(block)
    return os.path.getsize(local_path)


# Deliberately `def`, not `async def`.
#
# Everything this handler does blocks: downloading the media, writing it to
# disk, and running Whisper. On the event loop -- which is where an `async def`
# handler runs -- that would freeze the whole service for the duration, so a
# second transcription could not start and /health would stop answering while
# the first one ran.
#
# A plain `def` handler is run in FastAPI's thread pool instead, so blocking is
# contained to one thread and concurrent transcriptions actually overlap. The
# other two services are sync for the same reason.
@router.post("/transcribe", response_model=TranscribeResponse)
def transcribe_endpoint(
    session_id: str = Form(...),
    file_id: str = Form(...),
    filename: str = Form(...),
    url: str | None = Form(None),
    file: UploadFile | None = File(None),
) -> TranscribeResponse:
    """Transcribe a video and hand back chunk records.

    The monolith holds an HTTP connection open the whole time this runs. When
    it sends a `url` that connection carries almost nothing, so the only thing
    on it is the transcription itself. When it uploads a `file`, the connection
    also has to carry the video, and on a long one that is what eventually
    times out -- limit 04 in the design document.
    """
    if not url and file is None:
        raise HTTPException(status_code=400, detail="send either 'url' or 'file'")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            local_path = os.path.join(tmp, os.path.basename(filename) or "media")

            if url:
                log.info("fetching %s from storage", filename)
                size = _fetch_to(url, local_path)
                log.info("fetched %s (%.1f MB) directly — the monolith never "
                         "held it", filename, size / 1_048_576)
            else:
                # Stream to disk rather than reading it all into memory.
                with open(local_path, "wb") as handle:
                    shutil.copyfileobj(file.file, handle, DOWNLOAD_CHUNK)
                size = os.path.getsize(local_path)
                log.info("received %s (%.1f MB) as an upload", filename,
                         size / 1_048_576)

            log.info("transcribing — this can take a while")
            segments, seconds = transcribe.transcribe_file(local_path)
            log.info("transcribed %.1fs of audio into %d segments", seconds, len(segments))

            chunks = transcribe.segments_to_chunks(
                segments, session_id=session_id, file_id=file_id, source=filename
            )
            transcript = "\n".join(s["text"] for s in segments)

            return TranscribeResponse(
                chunks=chunks,
                chunk_count=len(chunks),
                seconds=seconds,
                transcript=transcript,
            )
    except requests.RequestException as exc:
        log.exception("could not fetch the media")
        raise HTTPException(
            status_code=502,
            detail=f"could not fetch the media from storage: {exc}",
        ) from exc
    except Exception as exc:
        log.exception("transcription failed")
        raise HTTPException(status_code=500, detail=f"transcription failed: {exc}") from exc


SERVICE_NAME = "stt"

app.include_router(router)

# --- pulling jobs ------------------------------------------------------------
#
# The service does two things at once now. It still answers HTTP calls, and it
# also runs a thread that asks the backend for queued work. Both use the same
# model in the same process, which is the point -- a separate program would
# mean a second copy of the weights on the same card.

_poller = None


@app.on_event("startup")
def start_polling() -> None:
    """Begin pulling jobs, if a broker is configured.

    With no BROKER_URL this does nothing and the service behaves exactly as it
    did before -- useful when you want to test it on its own.
    """
    global _poller
    if not (settings.broker_url and settings.broker_token):
        log.info("no broker configured; %s answers HTTP only", SERVICE_NAME)
        return
    client = broker.BrokerClient(settings.broker_url, settings.broker_token)
    _poller = jobs.poller(client)
    _poller.start()
    log.info("pulling %s jobs from %s", jobs.JOB, settings.broker_url)


@app.on_event("shutdown")
def stop_polling() -> None:
    if _poller is not None:
        _poller.stop()

