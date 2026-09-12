"""The contract with the backend.

Unchanged from version 2 except two additions, both bounded and both about
*where the numbers go*, not about the model itself:

`EmbedQueryRequest`/`EmbedQueryResponse` back a new endpoint, `/embed_query`.
In version 2 the backend asked this service to *search* ("here is a question,
tell me the best-matching chunks") because the vectors lived here, in Chroma.
In v3 the vectors live in the backend's own Postgres, so all this service is
asked for now is the much smaller "here is a question, give me its vector" --
the comparison happens in SQL on the other side.

`EmbedRequest` gained nothing -- indexing still means "here are chunks, embed
them" -- but the *response* to that job, over in app.py/jobs.py, now also
writes the vectors it computed out to a presigned URL, not just into Chroma.
"""
from pydantic import BaseModel


class EmbedChunk(BaseModel):
    """One chunk record, as written in the backend's .jsonl files."""

    chunk_id: str
    text: str
    source: str = ""
    section: str = ""
    chunks_key: str = ""     # provenance only; the service never opens it


class EmbedRequest(BaseModel):
    session_id: str
    chunks: list[EmbedChunk]


class EmbedResponse(BaseModel):
    session_id: str
    collection: str
    indexed: int


class EmbedQueryRequest(BaseModel):
    """New in v3. One question, going through the same model the documents
    did -- it has to, or the two are not comparable."""

    query: str


class EmbedQueryResponse(BaseModel):
    vector: list[float]


class RetrieveRequest(BaseModel):
    session_id: str
    query: str
    top_k: int = 4


class Hit(BaseModel):
    chunk_id: str
    chunks_key: str      # where to find the chunk's text -- the backend fetches it
    score: float
    source: str = ""
    section: str = ""


class RetrieveResponse(BaseModel):
    hits: list[Hit]
