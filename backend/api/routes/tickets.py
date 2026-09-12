"""Minting a ticket for the event stream.

New in v3, and it exists for one reason: a plain browser `EventSource` cannot
send an `Authorization` header, so it cannot carry a Cognito ID token the way
every other call does (see frontend/src/api.js). `EventSource` *can* carry a
query string, though, so the frontend exchanges its token for a short-lived
ticket through this ordinary, authenticated endpoint first
(frontend/src/useSessionEvents.js), then opens the stream with
`?ticket=...` instead of a header.

Deliberately not single-use. A native `EventSource` reconnects on its own
after a network blip, and a single-use ticket would make that reconnect fail
immediately against an already-consumed value — a real failure mode, not a
hypothetical one. A short TTL (`sse_ticket_seconds`, default five minutes) gets
the property the ticket exists for — never putting a long-lived JWT in a
query string, where it could end up in a proxy or access log — without
breaking reconnect.
"""
import json
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException

from shared import models
from shared.auth import require_user
from shared.config import get_settings
from shared.db import get_session
from shared.events import async_client
from shared.schemas import EventTicketOut

router = APIRouter(prefix="/sessions", tags=["events"])
log = logging.getLogger(__name__)
settings = get_settings()


def _ticket_key(ticket: str) -> str:
    return f"sse_ticket:{ticket}"


@router.post("/{session_id}/events/ticket", response_model=EventTicketOut)
async def create_ticket(session_id: str, owner_id: str = Depends(require_user),
                        db=Depends(get_session)) -> EventTicketOut:
    session = await db.get(models.Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")
    if session.owner_id != owner_id:
        raise HTTPException(status_code=403, detail="not your session")

    ticket = secrets.token_urlsafe(32)
    client = async_client()
    try:
        await client.set(
            _ticket_key(ticket),
            json.dumps({"session_id": session_id, "owner_id": owner_id}),
            ex=settings.sse_ticket_seconds,
        )
    finally:
        await client.aclose()

    return EventTicketOut(ticket=ticket, expires_in=settings.sse_ticket_seconds)


async def check_ticket(session_id: str, ticket: str) -> bool:
    """Used by routes/events.py: does this ticket grant access to this session?"""
    client = async_client()
    try:
        raw = await client.get(_ticket_key(ticket))
    finally:
        await client.aclose()
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return data.get("session_id") == session_id
