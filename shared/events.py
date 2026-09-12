"""Redis: the conversation window, and the channel that carries progress.

Two jobs, and the second is what makes streaming possible across a fleet.

**The conversation window.** Version 1 kept the last few turns in a Python
dictionary, which worked because there was one process. With many API tasks and
many workers, a user's history has to live somewhere all of them can see.

**Pub/sub.** A worker generates an answer, but the browser's connection is held
by some API task, and they are not the same process and never will be. The
worker publishes to a channel named for the session; whichever API task holds
that connection is subscribed and relays what arrives. Without this, streaming
only works when you have exactly one API instance — which is to say, it does
not work.

The API side is async (it holds these connections on the event loop); the
worker side is synchronous. Hence two clients over one server.
"""
import json
import logging
from typing import Any

import redis
import redis.asyncio as aioredis

from .config import get_settings

log = logging.getLogger(__name__)
settings = get_settings()

sync_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)


def async_client() -> aioredis.Redis:
    """A fresh async client. The API makes one per subscription."""
    return aioredis.Redis.from_url(settings.redis_url, decode_responses=True)


# --- the conversation window -------------------------------------------------

WINDOW = settings.history_turns * 2      # a question and an answer per turn


def _history_key(session_id: str) -> str:
    return f"chat:{session_id}"


def push_turn(session_id: str, role: str, content: str) -> None:
    """Remember one turn, best-effort.

    Called after the answer is already committed to the database
    (`workers/chat/main.py::handle`), so a failure here must never look like
    the *answer* failed — it is a cache of recent history, not the record.
    Same reasoning, and now the same try/except, as `publish()` below: losing
    a Redis write should cost this session its short-term memory for one
    turn, not get the just-generated answer overwritten with a failure
    message by the caller's exception handler.
    """
    key = _history_key(session_id)
    try:
        pipe = sync_client.pipeline()
        pipe.rpush(key, json.dumps({"role": role, "content": content}))
        pipe.ltrim(key, -WINDOW, -1)
        pipe.expire(key, 60 * 60 * 24)   # a cache, not the truth; let it lapse
        pipe.execute()
    except redis.RedisError:
        log.warning("could not push a conversation turn for %s", session_id, exc_info=True)


def history(session_id: str) -> list[dict[str, str]]:
    """The window, oldest first, ready to drop into a prompt."""
    return [json.loads(item) for item in sync_client.lrange(_history_key(session_id), 0, -1)]


# --- progress and tokens -----------------------------------------------------

def channel(session_id: str) -> str:
    return f"events:{session_id}"


def publish(session_id: str, event: dict[str, Any]) -> None:
    """Announce something to whoever is watching this session.

    Fire and forget. If nobody is listening the event is dropped, which is
    correct — the database still receives everything that matters, so a browser
    that was not connected can recover the result by asking for it.
    """
    try:
        sync_client.publish(channel(session_id), json.dumps(event))
    except redis.RedisError:
        # Never fail a job because the notification did not land.
        log.warning("could not publish an event for %s", session_id, exc_info=True)


# Event names, so the producer and the browser agree on them.
FILE_PROGRESS = "file"          # a file changed status
SESSION_PROGRESS = "session"    # the session changed status
MESSAGE_TOKEN = "token"         # a fragment of an answer
MESSAGE_STATUS = "message"      # a message changed status
