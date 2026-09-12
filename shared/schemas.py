"""The exact shape of every request and response.

Unchanged from version 2 except one addition: a ticket for the event stream,
because a plain browser `EventSource` cannot carry the `Authorization` header
Cognito otherwise requires on every call. See api/routes/tickets.py and
api/routes/events.py.
"""
from datetime import datetime

from pydantic import BaseModel, Field


# --- sessions ---------------------------------------------------------------

class SessionOut(BaseModel):
    session_id: str


class FileStatus(BaseModel):
    file_id: str
    filename: str
    kind: str
    status: str
    chunk_count: int = 0
    error: str | None = None


class SessionStatus(BaseModel):
    session_id: str
    status: str
    error: str | None = None
    files_total: int = 0
    files_done: int = 0
    files: list[FileStatus] = []


# --- uploads ----------------------------------------------------------------

class UploadRequestFile(BaseModel):
    filename: str = Field(min_length=1, max_length=400)
    content_type: str = "application/octet-stream"
    size: int = 0


class UploadRequest(BaseModel):
    files: list[UploadRequestFile] = Field(min_length=1, max_length=50)


class UploadTarget(BaseModel):
    file_id: str
    filename: str
    kind: str
    key: str
    upload_url: str


class UploadResponse(BaseModel):
    targets: list[UploadTarget]


class RegisterFile(BaseModel):
    file_id: str


class RegisterRequest(BaseModel):
    files: list[RegisterFile]


class RegisterResponse(BaseModel):
    session_id: str
    status: str
    files: int


# --- chat -------------------------------------------------------------------

class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class ChatAccepted(BaseModel):
    message_id: str


class Source(BaseModel):
    chunk_id: str
    source: str
    section: str | None = None
    score: float | None = None
    text: str | None = None


class MessageOut(BaseModel):
    id: str
    role: str
    content: str | None
    status: str
    sources: list[Source] | None = None
    created_at: datetime


# --- health and config ------------------------------------------------------

class ServiceUrls(BaseModel):
    embedding: str = Field(min_length=1, max_length=500)
    stt: str = Field(min_length=1, max_length=500)
    llm: str = Field(min_length=1, max_length=500)


class ServiceConfigOut(BaseModel):
    urls: dict[str, str]
    healthy: dict[str, bool]
    ready: bool


# --- the event stream (new in v3) --------------------------------------------
#
# A native EventSource cannot send an Authorization header, so the browser
# exchanges its Cognito ID token for a short-lived ticket through a normal,
# authenticated request first, then puts the ticket in the stream URL's query
# string instead of a header.

class EventTicketOut(BaseModel):
    ticket: str
    expires_in: int


# --- the broker --------------------------------------------------------------
#
# The only shapes the GPU ever sees. Everything it needs to do a job is in
# `JobOffer.body`, including the presigned URLs -- so it never learns a bucket
# name, a queue name, or a region. Unchanged from version 2.

class ClaimRequest(BaseModel):
    job: str                     # "stt" or "embed"
    worker: str = "gpu"          # free-form; only used in logs


class JobOffer(BaseModel):
    lease: str                   # opaque; hand it back to heartbeat and complete
    job: str
    attempt: int                 # how many times this job has been delivered
    body: dict


class JobLease(BaseModel):
    lease: str


class JobComplete(BaseModel):
    lease: str
    job: str
    session_id: str
    file_id: str
    ok: bool
    indexed: int | None = None   # embed jobs: how many chunks were embedded
    seconds: float | None = None
    error: str | None = None
