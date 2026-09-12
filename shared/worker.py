"""The consume loop both workers share.

Moved here from backend/workers/base.py in version 2 — unchanged logic. It
lives in `shared/` now because `workers/ingest` and `workers/chat` are
separately built, separately deployed ECS Fargate services in v3 (no longer
one image differentiated by a `command:`), and this loop is the one piece of
code both still need.

Long-poll the queue, hand each message to a handler, delete it if the handler
succeeds, and leave it alone if it does not — an untouched message becomes
visible again when its timeout expires and someone else tries it. After enough
failures SQS moves it to the dead-letter queue by itself.

Two details that matter more than they look:

**A heartbeat.** A job that takes longer than the visibility timeout would be
handed to a second worker while the first is still working on it. So the
handler is run on a thread and the main loop extends the message's visibility
while it is going.

**Graceful shutdown.** ECS sends SIGTERM, then SIGKILL after `stopTimeout`. We
stop pulling new work immediately and let the message in progress finish, so a
rolling deploy does not create a duplicate of every in-flight job.
"""
import logging
import signal
import threading
import time
from collections.abc import Callable

from . import queues
from .config import get_settings

log = logging.getLogger(__name__)
settings = get_settings()

Handler = Callable[[dict], None]


class Worker:
    def __init__(self, name: str, queue_url: str, handler: Handler,
                 heartbeat_seconds: int = 60):
        if not queue_url:
            raise SystemExit(f"{name}: no queue url configured")
        self.name = name
        self.queue_url = queue_url
        self.handler = handler
        self.heartbeat_seconds = heartbeat_seconds
        self._stopping = threading.Event()

    # --- lifecycle -----------------------------------------------------------

    def _install_signals(self) -> None:
        def stop(signum, _frame):
            log.info("%s: signal %s — finishing the current message, then exiting",
                     self.name, signum)
            self._stopping.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    def run(self) -> None:
        self._install_signals()
        log.info("%s: consuming %s", self.name, self.queue_url.rsplit("/", 1)[-1])

        while not self._stopping.is_set():
            try:
                messages = queues.receive(self.queue_url)
            except Exception:                    # noqa: BLE001
                log.exception("%s: could not read the queue; backing off", self.name)
                self._stopping.wait(5)
                continue

            for index, message in enumerate(messages):
                if self._stopping.is_set():
                    # Do not start new work during shutdown. Hand the rest back
                    # immediately rather than letting them wait out the full
                    # visibility timeout before anyone else can take them.
                    self._release(messages[index:])
                    break
                self._handle(message)

        log.info("%s: stopped", self.name)

    def _release(self, messages: list[queues.Message]) -> None:
        for message in messages:
            try:
                queues.release(message)
            except Exception:                    # noqa: BLE001
                # It will reappear when its visibility expires anyway.
                log.debug("%s: could not release a message early", self.name)

    # --- one message ---------------------------------------------------------

    def _handle(self, message: queues.Message) -> None:
        started = time.time()
        done = threading.Event()
        failure: list[BaseException] = []

        def work():
            try:
                self.handler(message.body)
            except BaseException as exc:         # noqa: BLE001
                failure.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        # Keep the message ours for as long as the handler is running.
        while not done.wait(timeout=self.heartbeat_seconds):
            try:
                queues.extend(message, settings.queue_visibility_seconds)
                log.debug("%s: still working (%.0fs)", self.name, time.time() - started)
            except Exception:                    # noqa: BLE001
                log.warning("%s: could not extend visibility", self.name, exc_info=True)
                break

        thread.join(timeout=5)
        elapsed = time.time() - started

        if not done.is_set():
            # The heartbeat loop gave up -- extending visibility failed -- but
            # the handler is still going. Deleting now would acknowledge work
            # that may yet fail, and nothing would ever retry it. Leave the
            # message alone; it becomes visible again on its own.
            log.error("%s: handler still running after %.1fs; leaving the message "
                      "for redelivery", self.name, elapsed)
            return

        if failure:
            # Do not delete. SQS makes it visible again, and after the queue's
            # configured number of attempts moves it to the dead-letter queue.
            log.error("%s: message failed after %.1fs (attempt %d): %s",
                      self.name, elapsed, message.receive_count, failure[0])
            return

        try:
            queues.delete(message)
        except Exception:                        # noqa: BLE001
            # The work is done but the acknowledgement failed, so it will be
            # delivered again. This is exactly why handlers must be idempotent.
            log.warning("%s: could not delete a completed message", self.name, exc_info=True)

        log.info("%s: done in %.1fs", self.name, elapsed)
