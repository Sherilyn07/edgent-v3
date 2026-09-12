"""Pulling work from the broker, instead of waiting to be called.

Read this file before the two handlers that use it. It is the whole of the GPU
side of the pattern, and it is deliberately small.

The old way, and why it hurt
----------------------------
The backend used to call us: `POST /transcribe`, and then hold that connection
open for however long a twenty-minute video takes. Everything about that is
fragile. The connection has to survive the whole job. We need a public address
for the backend to reach, which on Colab means a tunnel whose hostname changes
every restart. If we crash half way, the work is simply gone — nobody wrote
down that it was ever started. And if fifty videos arrive at once, we get fifty
simultaneous connections rather than a queue.

The new way
-----------
We ask *them* for work.

    claim      "anything to do?"    -> a job, or nothing
    heartbeat  "still going"        -> sent while we work
    complete   "done" / "failed"    -> the outcome

Three things fall out of turning the arrow around, and they are the reason the
pattern exists:

**No credentials.** We never talk to AWS. The backend holds the keys and talks
to the queue on our behalf. We hold one token that lets us ask for a job and
nothing else — it cannot read a bucket, list a queue, or touch another
session's data. If it leaks, one line of the backend's `.env` revokes it.

**No inbound address.** Every connection starts here and goes out. Colab
allows that freely, and it means this half of the system needs no tunnel at
all — no ephemeral hostname, nothing to paste into a screen, no
hundred-second proxy limit.

**Nothing is lost.** A job is not acknowledged until we say it finished. If
this runtime dies mid-transcription, the job becomes visible again and is
handed out afresh. Compare that with an HTTP call, where a dropped connection
takes the work with it.

How we touch storage without credentials
----------------------------------------
Each job arrives with **presigned URLs** already in it: a link to read the
input, and where it applies, a link to write the output. A presigned URL is a
normal https address with a signature attached that says "whoever holds this
may do this one thing to this one object until this time." So we use plain
`requests.get` and `requests.put` and never learn a bucket name.
"""
import logging
import threading
import time

import requests

log = logging.getLogger("broker")

# Sent while a job runs, so the backend keeps extending our claim on it. Well
# under the visibility timeout on the queue, so a slow network hiccup does not
# cost us the job.
HEARTBEAT_SECONDS = 30

# How long to wait after a network error before asking again. The backend may
# simply be restarting.
RETRY_SECONDS = 5


class BrokerClient:
    """The three calls, and nothing else."""

    def __init__(self, base_url: str, token: str, timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def claim(self, job: str) -> dict | None:
        """Ask for one job. Returns None when there is nothing to do.

        This call blocks for up to twenty seconds on the backend, because that
        is SQS long polling — one waiting call instead of a tight loop of empty
        ones. Far cheaper, and it reacts the instant work appears.
        """
        response = self.session.post(f"{self.base}/broker/claim",
                                     json={"job": job}, timeout=self.timeout)
        response.raise_for_status()
        # 204 means the queue was empty. Ask again.
        return response.json() if response.status_code == 200 else None

    def heartbeat(self, lease: str) -> None:
        self.session.post(f"{self.base}/broker/heartbeat",
                          json={"lease": lease}, timeout=30).raise_for_status()

    def complete(self, lease: str, job: str, body: dict, *, ok: bool,
                 error: str | None = None, **extra) -> None:
        payload = {
            "lease": lease,
            "job": job,
            "session_id": body.get("session_id", ""),
            "file_id": body.get("file_id", ""),
            "ok": ok,
            "error": error,
            **extra,
        }
        self.session.post(f"{self.base}/broker/complete",
                          json=payload, timeout=60).raise_for_status()


class JobPoller(threading.Thread):
    """Claim, run, report — for ever, on its own thread.

    One of these per job type. They run *inside* the existing service process
    rather than as a separate program, for a practical reason: the model is
    already loaded here, and a second process would mean a second copy of it on
    the same card.
    """

    def __init__(self, client: BrokerClient, job: str, handler, name: str | None = None):
        super().__init__(name=name or f"poll-{job}", daemon=True)
        self.client = client
        self.job = job
        self.handler = handler
        self._stopping = threading.Event()

    def stop(self) -> None:
        self._stopping.set()

    def run(self) -> None:
        log.info("%s: polling the broker for %s jobs", self.name, self.job)
        while not self._stopping.is_set():
            try:
                offer = self.client.claim(self.job)
            except Exception as exc:              # noqa: BLE001
                # The backend may be restarting, or the tunnel may have moved.
                # Nothing is lost by waiting: the job stays on the queue.
                log.warning("%s: could not reach the broker (%s); retrying in %ds",
                            self.name, exc, RETRY_SECONDS)
                self._stopping.wait(RETRY_SECONDS)
                continue

            if offer is None:
                continue                          # empty queue; ask again
            self._run_one(offer)

        log.info("%s: stopped", self.name)

    def _run_one(self, offer: dict) -> None:
        lease, body = offer["lease"], offer["body"]
        started = time.time()
        log.info("%s: starting a job (attempt %d)", self.name, offer.get("attempt", 1))

        # The handler runs on its own thread so this one can keep the claim
        # alive. Exactly the same shape as the backend's worker loop -- a long
        # job must keep saying "still mine" or the queue hands it to somebody
        # else while it is still running.
        result: dict = {}
        failure: list[BaseException] = []
        done = threading.Event()

        def work():
            try:
                result.update(self.handler(body) or {})
            except BaseException as exc:          # noqa: BLE001
                failure.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        while not done.wait(timeout=HEARTBEAT_SECONDS):
            try:
                self.client.heartbeat(lease)
                log.debug("%s: still working (%.0fs)", self.name, time.time() - started)
            except Exception:                     # noqa: BLE001
                log.warning("%s: heartbeat failed; the job may be redelivered",
                            self.name, exc_info=True)
                break

        elapsed = round(time.time() - started, 1)

        if not done.is_set():
            # The heartbeat loop gave up -- a heartbeat call failed -- but the
            # handler thread is still running. Calling complete() now would
            # report an outcome (success or failure) for work that has not
            # actually finished, and specifically ok=True with an empty
            # `result` if it happens to land in the "else" branch below. Say
            # nothing and let the lease's own visibility timeout expire; the
            # job becomes visible again and is retried, which is the whole
            # reason it is on a queue. Exactly the guard
            # `backend/workers/base.py` (now `shared/worker.py`) already has
            # for the same race -- this file's docstring claims the two loops
            # are "exactly the same shape," and this is what makes that true.
            log.error("%s: handler still running after a failed heartbeat; "
                      "leaving the job for redelivery rather than reporting "
                      "an outcome for unfinished work", self.name)
            return

        try:
            if failure:
                log.error("%s: job failed after %ss: %s", self.name, elapsed, failure[0])
                self.client.complete(lease, self.job, body, ok=False,
                                     error=str(failure[0])[:1000], seconds=elapsed)
            else:
                log.info("%s: job done in %ss", self.name, elapsed)
                self.client.complete(lease, self.job, body, ok=True,
                                     seconds=elapsed, **result)
        except Exception:                         # noqa: BLE001
            # We could not report the outcome. The job was never acknowledged,
            # so it comes back and is done again -- which is safe precisely
            # because both handlers are idempotent.
            log.warning("%s: could not report the outcome; it will be redelivered",
                        self.name, exc_info=True)


# --- helpers the handlers share ----------------------------------------------

def download(url: str, path: str, timeout: int = 900) -> int:
    """Fetch a presigned URL onto disk, in pieces.

    Streamed rather than read into memory: this is how a two-gigabyte video
    arrives on a machine that should not need two gigabytes of RAM to hold it.
    """
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with open(path, "wb") as handle:
            for block in response.iter_content(chunk_size=1024 * 1024):
                handle.write(block)
    import os
    return os.path.getsize(path)


def upload_json(url: str, payload: dict, timeout: int = 300) -> None:
    """Write a result back through a presigned PUT link.

    The content type must match the one the backend signed, or S3 rejects the
    signature -- a mismatch here is the classic cause of a 403 that looks like
    a permissions problem and is not.
    """
    import json
    response = requests.put(url, data=json.dumps(payload).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                            timeout=timeout)
    response.raise_for_status()


def iter_jsonl(url: str, timeout: int = 300):
    """Stream a .jsonl file from a presigned URL, one record at a time."""
    import json
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if line and line.strip():
                yield json.loads(line)
