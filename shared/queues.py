"""SQS: the change that the rest of version 2 is built on.

A producer writes a message; a consumer picks it up and the message becomes
invisible but is not deleted. Only when the consumer confirms success does it
go away. If the consumer dies first, the message reappears and somebody else
takes it.

That one behaviour replaces `background.add_task`, which was a queue with no
durability, no retry, and no consumer you could scale.

Two queues, deliberately:

    ingest   bulk work, one message per file, throughput matters
    chat     interactive work, one message per question, latency matters

Sharing one would put somebody's fifty-file upload in front of everybody's
questions. Each has a dead-letter queue so a message that cannot be processed
moves aside instead of retrying for ever; that is configured on the queue, not
here.

Delivery is at least once, so every handler must be idempotent. Ours are:
chunk keys are deterministic and written with an upsert, and message rows are
updated by primary key.
"""
import json
import logging
from dataclasses import dataclass
from typing import Any

import boto3

from .config import get_settings

log = logging.getLogger(__name__)
settings = get_settings()


def _client():
    return boto3.client(
        "sqs",
        region_name=settings.aws_region,
        endpoint_url=settings.sqs_endpoint_url,
    )


client = _client()


@dataclass
class Message:
    """One message, plus the handle needed to delete or extend it."""

    body: dict[str, Any]
    receipt_handle: str
    receive_count: int
    queue_url: str


# --- producing ---------------------------------------------------------------

def send(queue_url: str, body: dict[str, Any], group_id: str | None = None) -> None:
    kwargs: dict[str, Any] = {"QueueUrl": queue_url, "MessageBody": json.dumps(body)}
    if group_id and queue_url.endswith(".fifo"):
        kwargs["MessageGroupId"] = group_id
    client.send_message(**kwargs)


def send_many(queue_url: str, bodies: list[dict[str, Any]]) -> None:
    """Up to ten per call, which is SQS's batch limit."""
    for start in range(0, len(bodies), 10):
        batch = bodies[start : start + 10]
        client.send_message_batch(
            QueueUrl=queue_url,
            Entries=[{"Id": str(i), "MessageBody": json.dumps(b)} for i, b in enumerate(batch)],
        )


# --- consuming ---------------------------------------------------------------

def receive(queue_url: str, max_messages: int | None = None) -> list[Message]:
    """Long-poll for work.

    Long polling matters: one twenty-second call that returns the moment a
    message arrives, rather than a tight loop of empty requests. It is both
    faster to react and dramatically cheaper.
    """
    response = client.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=max_messages or settings.queue_batch_size,
        WaitTimeSeconds=settings.queue_wait_seconds,
        VisibilityTimeout=settings.queue_visibility_seconds,
        AttributeNames=["ApproximateReceiveCount"],
    )
    out = []
    for raw in response.get("Messages", []):
        try:
            body = json.loads(raw["Body"])
        except json.JSONDecodeError:
            log.exception("undecodable message; leaving it for the dead-letter queue")
            continue
        out.append(Message(
            body=body,
            receipt_handle=raw["ReceiptHandle"],
            receive_count=int(raw.get("Attributes", {}).get("ApproximateReceiveCount", 1)),
            queue_url=queue_url,
        ))
    return out


def delete(message: Message) -> None:
    """Acknowledge. Until this is called the message is only hidden."""
    client.delete_message(QueueUrl=message.queue_url, ReceiptHandle=message.receipt_handle)


def extend(message: Message, seconds: int) -> None:
    """Ask for more time on a job that is taking longer than expected.

    Without this, a transcription that outlives the visibility timeout is
    handed to a second worker while the first is still working on it.
    """
    client.change_message_visibility(
        QueueUrl=message.queue_url,
        ReceiptHandle=message.receipt_handle,
        VisibilityTimeout=seconds,
    )


def release(message: Message) -> None:
    """Make a message visible again immediately, to retry it without waiting."""
    extend(message, 0)


def depth(queue_url: str) -> dict[str, int]:
    """Visible and in-flight counts — the numbers you autoscale on."""
    attrs = client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages",
                        "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return {
        "waiting": int(attrs.get("ApproximateNumberOfMessages", 0)),
        "in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
    }
