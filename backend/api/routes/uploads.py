"""Handing out upload links, and putting work on the queue.

Unchanged in shape from version 2 — two handlers, both measured in
milliseconds, no file bytes pass through here — plus an ownership check on
each, now that a session has an owner.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from shared import models, queues, storage
from shared.auth import require_user
from shared.config import get_settings
from shared.convert import kind_for_filename
from shared.db import get_session
from shared.schemas import (
    RegisterRequest,
    RegisterResponse,
    UploadRequest,
    UploadResponse,
    UploadTarget,
)

router = APIRouter(prefix="/sessions", tags=["uploads"])
log = logging.getLogger(__name__)
settings = get_settings()


async def _owned_session(session_id: str, owner_id: str, db: AsyncSession) -> models.Session:
    session = await db.get(models.Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")
    if session.owner_id != owner_id:
        raise HTTPException(status_code=403, detail="not your session")
    return session


@router.post("/{session_id}/uploads", response_model=UploadResponse)
async def create_uploads(session_id: str, body: UploadRequest,
                         owner_id: str = Depends(require_user),
                         db: AsyncSession = Depends(get_session)) -> UploadResponse:
    """Create a File row per upload and hand back one presigned URL each."""
    await _owned_session(session_id, owner_id, db)

    for spec in body.files:
        if spec.size and spec.size > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{spec.filename} is larger than the "
                       f"{settings.max_upload_bytes // (1024 ** 3)} GiB limit",
            )

    rows: list[models.File] = []
    prepared: list[tuple[str, str, str, str]] = []   # (file_id, filename, kind, key)

    for spec in body.files:
        kind = kind_for_filename(spec.filename)
        if kind is None:
            raise HTTPException(status_code=400,
                                detail=f"unsupported file type: {spec.filename}")

        file_id = models.new_id()
        key = storage.raw_key(session_id, file_id, spec.filename)
        rows.append(models.File(
            id=file_id, session_id=session_id, filename=spec.filename,
            kind=kind, raw_key=key, status=models.FILE_PENDING,
            size_bytes=spec.size or 0,
        ))
        prepared.append((file_id, spec.filename, kind, key))

    # boto3 is synchronous; signing is pure computation but it is not ours to
    # block the loop with. The files are independent of each other, so presign
    # them concurrently rather than one round trip at a time — the same
    # asyncio.gather pattern routes/config.py's _probe() already uses for the
    # same reason (independent I/O, no reason to serialize it).
    urls = await asyncio.gather(*[
        run_in_threadpool(storage.presign_put, key, spec.content_type)
        for spec, (_, _, _, key) in zip(body.files, prepared)
    ])
    targets = [
        UploadTarget(file_id=file_id, filename=filename, kind=kind, key=key, upload_url=url)
        for (file_id, filename, kind, key), url in zip(prepared, urls)
    ]

    db.add_all(rows)
    await db.commit()
    return UploadResponse(targets=targets)


@router.post("/{session_id}/files/register", response_model=RegisterResponse)
async def register_files(session_id: str, body: RegisterRequest,
                         owner_id: str = Depends(require_user),
                         db: AsyncSession = Depends(get_session)) -> RegisterResponse:
    """The uploads have landed. Put one message per file on the ingest queue."""
    session = await _owned_session(session_id, owner_id, db)

    wanted = {f.file_id for f in body.files}
    rows = (await db.execute(
        select(models.File)
        .where(models.File.session_id == session_id, models.File.id.in_(wanted))
    )).scalars().all()

    if not rows:
        raise HTTPException(status_code=400, detail="none of those files belong to this session")

    missing = wanted - {row.id for row in rows}
    if missing:
        # A partial match used to be accepted silently, registering only the
        # files that happened to belong here and dropping the rest with no
        # signal to the caller. In normal operation the frontend always sends
        # back exactly the ids create_uploads() just handed it, so a mismatch
        # here means something is wrong with the caller's state -- surfacing
        # it as an error is what lets that get noticed and fixed, instead of
        # a session quietly processing fewer files than the user asked for.
        raise HTTPException(
            status_code=400,
            detail=f"{len(missing)} file id(s) do not belong to this session: "
                   f"{', '.join(sorted(missing))}",
        )

    session.status = models.SESSION_PROCESSING
    session.files_total = len(rows)
    session.files_done = 0
    session.error = None
    await db.commit()

    # One message per file. Fifty files become fifty parallel jobs across the
    # worker fleet rather than one sequential loop.
    messages = [
        {
            "session_id": session_id,
            "file_id": row.id,
            "filename": row.filename,
            "kind": row.kind,
            "raw_key": row.raw_key,
        }
        for row in rows
    ]
    await run_in_threadpool(queues.send_many, settings.ingest_queue_url, messages)
    log.info("queued %d files for session %s", len(messages), session_id)

    return RegisterResponse(session_id=session_id,
                            status=models.SESSION_PROCESSING,
                            files=len(rows))
