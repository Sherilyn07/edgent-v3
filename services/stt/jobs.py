"""Transcription as a pulled job rather than a call we answer.

The work is identical to what `/transcribe` does. What changed is who starts
it, and that changes everything around it:

    before   the backend called us and held the connection for twenty minutes
    now      we ask for a job, do it, write the result to storage, and report

`/transcribe` is still there and still works — it is useful for testing a
single file by hand. But nothing in the running system uses it any more.
"""
import logging
import os
import tempfile

import broker
from broker import download, upload_json
from . import transcribe
from .config import get_settings

log = logging.getLogger("stt.jobs")
settings = get_settings()

JOB = "stt"


def handle(body: dict) -> dict:
    """One video in, one transcript in storage, out.

    The job carries two signed links and no credentials: `media_url` to read
    the video, `result_url` to write the transcript. We never learn which
    bucket either of them points at.
    """
    filename = body.get("filename") or "media"
    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, os.path.basename(filename) or "media")

        size = download(body["media_url"], local, settings.download_timeout_seconds)
        log.info("fetched %s (%.1f MB) straight from storage", filename, size / 1_048_576)

        segments, seconds = transcribe.transcribe_file(local)
        log.info("transcribed %.0fs of audio into %d segments", seconds, len(segments))

        chunks = transcribe.segments_to_chunks(
            segments,
            session_id=body["session_id"],
            file_id=body["file_id"],
            source=filename,
        )

        # Written to storage, not returned. A long transcript is far larger
        # than a queue message may be, and the backend can read it at leisure.
        upload_json(body["result_url"], {
            "transcript": "\n".join(s["text"] for s in segments),
            "chunks": chunks,
            "chunk_count": len(chunks),
            "seconds": seconds,
        })

    # Reported back on the /complete call, so it shows up in the backend's log.
    return {"indexed": len(chunks)}


def poller(client: broker.BrokerClient) -> broker.JobPoller:
    return broker.JobPoller(client, JOB, handle, name="stt-jobs")
