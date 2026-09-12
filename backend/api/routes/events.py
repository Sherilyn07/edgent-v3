"""The event stream — what replaces polling. Unchanged mechanics from version
2 (Redis pub/sub fan-out across API tasks, the `open` frame before touching
Redis, the fifteen-second keepalive comment).

What changed: a `?ticket=` query parameter, checked against tickets.py, because
a plain browser `EventSource` cannot send the `Authorization` header every
other endpoint now requires. See tickets.py's docstring for why it is a
short-lived ticket rather than the JWT itself, and why it is not single-use.
"""
import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse

from shared import events
from shared.config import get_settings
from .tickets import check_ticket

router = APIRouter(prefix="/sessions", tags=["events"])
log = logging.getLogger(__name__)
settings = get_settings()


def _frame(event: str, data: dict) -> str:
    """One server-sent-event frame. The blank line is what ends it."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream(session_id: str, request: Request) -> AsyncIterator[str]:
    yield _frame("open", {"session_id": session_id})

    client = None
    pubsub = None
    try:
        client = events.async_client()
        pubsub = client.pubsub()
        await pubsub.subscribe(events.channel(session_id))

        while True:
            if await request.is_disconnected():
                break

            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=settings.sse_heartbeat_seconds,
            )

            if message is None:
                yield ": keepalive\n\n"          # a comment; the browser ignores it
                continue

            try:
                payload = json.loads(message["data"])
            except (TypeError, ValueError):
                continue

            yield _frame(payload.get("event", "message"), payload)
    except asyncio.CancelledError:
        raise
    except Exception:                            # noqa: BLE001
        log.exception("event stream failed for %s", session_id)
    finally:
        try:
            if pubsub is not None:
                await pubsub.unsubscribe(events.channel(session_id))
                await pubsub.aclose()
            if client is not None:
                await client.aclose()
        except Exception:                        # noqa: BLE001
            pass


@router.get("/{session_id}/events")
async def stream_events(session_id: str, ticket: str, request: Request) -> StreamingResponse:
    """Subscribe to everything happening in one session.

    `ticket` is required and minted by `POST /sessions/{id}/events/ticket`
    (tickets.py) — not the Cognito token itself, since a browser EventSource
    cannot attach one as a header.
    """
    if not await check_ticket(session_id, ticket):
        raise HTTPException(status_code=403, detail="missing or expired ticket")

    return StreamingResponse(
        _stream(session_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx buffers responses by default, which would hold the stream
            # until it was complete and defeat the entire mechanism.
            "X-Accel-Buffering": "no",
        },
    )
