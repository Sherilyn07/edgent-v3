"""How the workers talk to the three model services.

Same discipline as version 2 — a timeout on every call, retries with
exponential backoff and jitter, and a circuit breaker so a service that is
genuinely down stops being hammered. Only workers call these; the API never
does.

What changed from version 2
----------------------------
`retrieve()` is gone. Searching chunks used to be a direct call to the
embedding service's `/retrieve`, which searched its own Chroma store and
handed back keys and scores. Now the vectors live in our own Postgres
(`Chunk.embedding`, pgvector), so retrieval is a SQL query
(`shared/vectorstore.py::search`), not an HTTP call.

What the chat worker still needs from the embedding service is much smaller:
turn the user's *question* into one vector, so it can be compared against the
ones already stored. That is `embed_query()` below, calling a new endpoint
(`POST /embed_query`) that returns a single vector and nothing else — the
smallest change that keeps the embedding model itself untouched.
"""
import logging
import random
import threading
import time
from typing import Any

import requests

from . import services
from .config import get_settings

log = logging.getLogger(__name__)
settings = get_settings()


class ServiceError(RuntimeError):
    """A model service could not be reached, or refused the request."""


# --- circuit breaker ---------------------------------------------------------

class _Breaker:
    """Stop calling something that is clearly down.

    Per-process state, same as version 2: under Fargate with several worker
    tasks each has its own breaker, so this is not fleet-coordinated — a dead
    service still gets one probe per task rather than one for the whole
    fleet. That is a known, accepted limitation, not an oversight.
    """

    def __init__(self, threshold: int = 5, cooldown: float = 30.0):
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    def before(self, name: str) -> None:
        with self._lock:
            if self._failures < self.threshold:
                return
            if time.time() - self._opened_at < self.cooldown:
                raise ServiceError(f"{name} is unavailable (circuit open)")
            self._failures = self.threshold - 1      # let one probe through

    def succeeded(self) -> None:
        with self._lock:
            self._failures = 0

    def failed(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures == self.threshold:
                self._opened_at = time.time()


_breakers: dict[str, _Breaker] = {}
_breakers_lock = threading.Lock()


def _breaker(name: str) -> _Breaker:
    with _breakers_lock:
        return _breakers.setdefault(name, _Breaker())


def _request(name: str, url: str, **kwargs) -> dict[str, Any]:
    breaker = _breaker(name)
    breaker.before(name)

    kwargs.setdefault("timeout", settings.service_timeout_seconds)
    last: Exception | None = None

    for attempt in range(settings.service_retries):
        try:
            response = requests.post(url, **kwargs)
            if 400 <= response.status_code < 500:
                # We sent something wrong. Retrying will not help.
                breaker.succeeded()
                raise ServiceError(f"{name} rejected the request: "
                                   f"{response.status_code} {response.text[:200]}")
            response.raise_for_status()
            breaker.succeeded()
            return response.json()
        except ServiceError:
            raise
        except Exception as exc:                       # noqa: BLE001 — retry anything else
            last = exc
            breaker.failed()
            if attempt == settings.service_retries - 1:
                break
            delay = settings.service_backoff_seconds * (2 ** attempt) * (0.5 + random.random())
            log.warning("%s failed (%s); retrying in %.1fs", name, exc, delay)
            time.sleep(delay)

    raise ServiceError(f"{name} failed after {settings.service_retries} attempts: {last}")


# --- embedding ---------------------------------------------------------------

def embed_query(query: str) -> list[float]:
    """Turn a question into one vector, so it can be compared in SQL.

    A direct call on purpose: a person is waiting, and embedding one short
    string takes milliseconds. The comparison itself — finding the closest
    chunks — happens in Postgres, not here; see vectorstore.py::search.
    """
    body = _request("embedding", f"{services.url('embedding')}/embed_query",
                    json={"query": query})
    return body["vector"]


# Indexing is not here at all. It is bulk work nobody is waiting on, and the
# GPU pulls those jobs for itself through the broker (see
# api/routes/broker.py). Speech to text is the same story. Only the calls
# where somebody is watching a spinner are direct HTTP from a worker.


# --- generation --------------------------------------------------------------

def generate(prompt: str, max_new_tokens: int | None = None,
             temperature: float | None = None) -> dict[str, Any]:
    """Answer a prompt.

    A direct call, not a queue: latency decides how this feels, and when the
    service learns to stream, a queue could not carry it.
    """
    return _request("llm", f"{services.url('llm')}/generate",
                    json={"prompt": prompt,
                          "max_new_tokens": max_new_tokens or settings.max_new_tokens,
                          "temperature": settings.temperature
                          if temperature is None else temperature})


# --- health ------------------------------------------------------------------

def health(url: str) -> dict[str, Any] | None:
    """Ask a service what it is. Returns its /health body, or None if it is down."""
    try:
        response = requests.get(f"{url.rstrip('/')}/health", timeout=10)
        return response.json() if response.ok else None
    except (requests.RequestException, ValueError):
        return None
