"""Object storage. Real S3, in development and in production alike.

Unchanged from version 2 except one new key. Presigned URLs still carry work in
three directions: the browser uploads straight to the bucket, the GPU
downloads a video straight from it, and the GPU writes results straight back —
now including a vector file, not just a transcript.
"""
import logging

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import get_settings

log = logging.getLogger(__name__)
settings = get_settings()


def _client():
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        endpoint_url=settings.s3_endpoint_url,
        # Some S3-compatible stores need path-style addressing; real S3 does not.
        config=Config(s3={"addressing_style": "path"} if settings.s3_endpoint_url else {},
                      signature_version="s3v4"),
    )


client = _client()


# --- key layout, identical to version 2, plus vectors_key --------------------

def raw_key(session_id: str, file_id: str, filename: str) -> str:
    return f"sessions/{session_id}/raw/{file_id}/{filename}"


def text_key(session_id: str, file_id: str) -> str:
    return f"sessions/{session_id}/text/{file_id}.txt"


def transcript_key(session_id: str, file_id: str) -> str:
    """Where the GPU writes a finished transcript."""
    return f"sessions/{session_id}/transcript/{file_id}.json"


def chunks_key(session_id: str, file_id: str) -> str:
    """Where the GPU reads the chunks it is asked to index.

    They travel through storage rather than in the queue message because SQS
    caps a message at 256 KB, and a large document is far past that.
    """
    return f"sessions/{session_id}/chunks/{file_id}.jsonl"


def vectors_key(session_id: str, file_id: str) -> str:
    """Where the GPU writes the vectors it computed for this file's chunks.

    New in v3. The embedding service already computes these per batch (see
    services/embedding/jobs.py); this is the presigned PUT target it writes
    them to, alongside its existing chunks-indexed-into-Chroma path. The
    ingest worker's "vectorize" stage (workers/ingest/main.py) then reads this
    key and bulk-UPDATEs `chunks.embedding` in Postgres.

    A single JSON document — a list of `{"chunk_id", "embedding"}` — written
    with `broker.upload_json`, the same one-shot-PUT helper the STT service
    already uses for its transcript. Not `.jsonl`: unlike `chunks_key`, this
    file is never streamed line by line, so there is no reason to shape it
    that way.
    """
    return f"sessions/{session_id}/vectors/{file_id}.json"


# --- signed links ------------------------------------------------------------

def presign_put(key: str, content_type: str, expiry: int | None = None) -> str:
    return client.generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expiry or settings.presign_expiry_seconds,
    )


def presign_get(key: str, expiry: int | None = None) -> str:
    """A temporary read link, so another machine can fetch the object itself."""
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=expiry or settings.presign_expiry_seconds,
    )


# --- small reads and writes --------------------------------------------------

def put_text(key: str, body: str) -> None:
    client.put_object(Bucket=settings.s3_bucket, Key=key,
                      Body=body.encode("utf-8"), ContentType="text/plain; charset=utf-8")


def get_text(key: str) -> str:
    return client.get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read().decode("utf-8")


def upload_file(key: str, local_path: str, content_type: str = "text/plain; charset=utf-8") -> None:
    """Stream a file from disk into the bucket. Memory use is flat."""
    client.upload_file(local_path, settings.s3_bucket, key,
                       ExtraArgs={"ContentType": content_type})


def download_to(key: str, local_path: str) -> None:
    """Stream an object onto disk. Memory use is independent of file size."""
    client.download_file(settings.s3_bucket, key, local_path)


def exists(key: str) -> bool:
    try:
        client.head_object(Bucket=settings.s3_bucket, Key=key)
        return True
    except ClientError:
        return False


def head_size(key: str) -> int | None:
    """The object's size, or None if it is not there yet."""
    try:
        return int(client.head_object(Bucket=settings.s3_bucket, Key=key)["ContentLength"])
    except ClientError:
        return None


def ensure_bucket() -> None:
    """Create the bucket if it is missing. Used by local bootstrap only."""
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
        return
    except ClientError:
        pass
    kwargs = {"Bucket": settings.s3_bucket}
    if settings.aws_region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": settings.aws_region}
    client.create_bucket(**kwargs)
    log.info("created bucket %s", settings.s3_bucket)
