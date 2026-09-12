"""Create the four SQS queues and their dead-letter queues. Idempotent.

Run once, from anywhere that has AWS credentials — your laptop, or a one-off
ECS task run for the purpose:

    python -m scripts.bootstrap

New in v3: the queue name prefix is `edgentrag-v3-`, not version 2's
`edgentrag-`. That is deliberate, not cosmetic — the `embed` job's message
body gained a field (`vectors_url`) and the `ingest` queue gained a new stage
(`"vectorize"`) that a version 2 worker does not know how to handle. Separate
queues mean the two stacks can never cross-deliver a message the other
generation does not understand, which matters if v2 and v3 are ever running
at the same time during a cutover.

The S3 bucket already exists from version 1/2; this does not touch it.
Tables are created by Alembic (`python -m scripts.migrate`), not by this
script or by the application at start-up — see shared/db.py's docstring.
"""
import json
import logging
import secrets
import sys

from shared import queues
from shared.config import get_settings

logging.basicConfig(level="INFO", format="%(levelname)-7s %(message)s")
log = logging.getLogger("bootstrap")
settings = get_settings()

MAX_RECEIVES = settings.queue_max_receives
PREFIX = "edgentrag-v3"


def ensure_queue(name: str) -> str:
    """Create a queue and its dead-letter queue, and wire them together."""
    dlq = queues.client.create_queue(QueueName=f"{name}-dlq")["QueueUrl"]
    dlq_arn = queues.client.get_queue_attributes(
        QueueUrl=dlq, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]

    main = queues.client.create_queue(
        QueueName=name,
        Attributes={
            "VisibilityTimeout": str(settings.queue_visibility_seconds),
            "ReceiveMessageWaitTimeSeconds": str(settings.queue_wait_seconds),
            "MessageRetentionPeriod": str(4 * 24 * 60 * 60),      # four days
            "RedrivePolicy": json.dumps({
                "deadLetterTargetArn": dlq_arn,
                "maxReceiveCount": MAX_RECEIVES,
            }),
        },
    )["QueueUrl"]

    log.info("%-25s %s", name, main)
    log.info("%-25s %s  (after %d attempts)", f"{name}-dlq", dlq, MAX_RECEIVES)
    return main


def main() -> int:
    log.info("region: %s", settings.aws_region)

    # Four queues, in two pairs.
    #
    #   ingest, chat   drained by our own ECS Fargate worker services
    #   stt, embed     drained by the GPU, through the broker -- it never
    #                  sees these URLs, only the broker does
    urls = {name: ensure_queue(f"{PREFIX}-{name}")
            for name in ("ingest", "chat", "stt", "embed")}

    print("\nPut these in .env / the ECS task definitions:\n")
    for name, url in urls.items():
        print(f"{name.upper()}_QUEUE_URL={url}")

    if not settings.broker_token:
        print("\n# The GPU's token. It grants \"ask for a job\" and nothing else.")
        print(f"BROKER_TOKEN={secrets.token_hex(32)}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
