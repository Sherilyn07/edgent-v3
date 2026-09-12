"""Asking a question, and reading the conversation back.

Unchanged in shape from version 2, plus the ownership check every
session-scoped route now carries.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from shared import models, queues
from shared.auth import require_user
from shared.config import get_settings
from shared.db import get_session
from shared.schemas import ChatAccepted, ChatRequest, MessageOut

router = APIRouter(prefix="/sessions", tags=["chat"])
log = logging.getLogger(__name__)
settings = get_settings()


@router.post("/{session_id}/chat", response_model=ChatAccepted, status_code=202)
async def post_message(session_id: str, body: ChatRequest,
                       owner_id: str = Depends(require_user),
                       db: AsyncSession = Depends(get_session)) -> ChatAccepted:
    """Accept a question and put it on the chat queue.

    Two rows and a message, then `202`. The answer arrives on the event
    stream; the finished version is written to the database so a browser that
    was not connected can still recover it.
    """
    session = await db.get(models.Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")
    if session.owner_id != owner_id:
        raise HTTPException(status_code=403, detail="not your session")
    if session.status != models.SESSION_READY:
        raise HTTPException(status_code=409, detail="session has nothing indexed yet")

    question = models.Message(session_id=session_id, role=models.ROLE_USER,
                              content=body.content, status=models.MESSAGE_DONE)
    answer = models.Message(session_id=session_id, role=models.ROLE_ASSISTANT,
                            content=None, status=models.MESSAGE_PENDING)
    db.add_all([question, answer])
    await db.commit()

    await run_in_threadpool(queues.send, settings.chat_queue_url, {
        "session_id": session_id,
        "message_id": answer.id,
        "question": body.content,
    })

    return ChatAccepted(message_id=answer.id)


@router.get("/{session_id}/chat", response_model=list[MessageOut])
async def list_messages(session_id: str, owner_id: str = Depends(require_user),
                        db: AsyncSession = Depends(get_session)) -> list[MessageOut]:
    """The whole conversation, oldest first."""
    session = await db.get(models.Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")
    if session.owner_id != owner_id:
        raise HTTPException(status_code=403, detail="not your session")

    rows = (await db.execute(
        select(models.Message)
        .where(models.Message.session_id == session_id)
        .order_by(models.Message.created_at)
    )).scalars().all()

    return [
        MessageOut(id=r.id, role=r.role, content=r.content, status=r.status,
                   sources=r.sources, created_at=r.created_at)
        for r in rows
    ]
