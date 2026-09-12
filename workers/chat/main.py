"""The chat worker: retrieve, prompt, generate, publish, save.

A fully separate ECS Fargate service in v3, same as ingest — but its own,
smaller image: it never touches Docling, and as of v3 it never touches S3
either (see below).

What changed from version 2
----------------------------
Retrieval used to be an HTTP call to the embedding service's `/retrieve`,
which searched its own Chroma store and returned chunk keys and scores; a
second, separate database query then fetched the text those keys pointed to.

Now the vectors live beside the text, in our own Postgres. So:

    1. embed the question       -- one small HTTP call (shared/clients.embed_query)
    2. find the closest chunks  -- one SQL query (shared/vectorstore.search)

Two steps instead of "one HTTP call, then a second lookup, then a Python-side
re-sort, then an orphan check for a chunk that was indexed but never stored."
The orphan check in particular simply cannot happen any more:
`vectorstore.search` only ever returns rows that already have both a vector
and text, because they are the same row.

On streaming
------------
Unchanged from version 2: the plumbing is built for token-by-token output, but
the language model service still returns a finished string, so this still
emits progress events and then the answer in one piece. Still a deliberate
seam -- see version 2's TUTORIAL.md §5 for why generation stays a direct call
rather than a queue.

    python workers/chat/main.py
"""
import logging
import time

from shared import clients, events, models, vectorstore
from shared.config import get_settings
from shared.db import init_db, worker_session
from shared.worker import Worker

log = logging.getLogger(__name__)
settings = get_settings()


SYSTEM_INSTRUCTIONS = """You are answering questions about a specific set of documents.

Use only the numbered context below. If the context does not contain the answer, say so
plainly instead of guessing. Mention which source you used.
"""


def handle(body: dict) -> None:
    session_id = body["session_id"]
    message_id = body["message_id"]
    question = body["question"]

    with worker_session() as db:
        row = db.get(models.Message, message_id)
        if row is None:
            log.warning("no such message %s", message_id)
            return
        if row.status == models.MESSAGE_DONE:
            log.info("message %s is already answered; skipping", message_id)
            return

        row.status = models.MESSAGE_ANSWERING
        db.commit()
        _announce(session_id, row, stage="retrieving")

        try:
            started = time.time()

            # 1. the question, as a vector -- a direct call, milliseconds,
            #    somebody is waiting.
            vector = clients.embed_query(question)

            # 2. the closest chunks, text and score together -- one SQL
            #    query against our own database, not a second round trip.
            sources = vectorstore.search(db, session_id, vector, settings.top_k)
            _announce(session_id, row, stage="generating", sources=len(sources))

            # 3. the prompt, and the model.
            history = events.history(session_id)
            prompt = build_prompt(question, sources, history)
            result = clients.generate(prompt)
            content = result.get("content", "")

            # 4. publish, then persist. The stream is ephemeral; the database
            #    is the record, so a browser that was not connected -- or that
            #    reloaded -- can still recover the answer.
            publish_token(session_id, message_id, content)

            row.content = content
            row.status = models.MESSAGE_DONE
            row.sources = sources
            db.commit()

            _announce(session_id, row, stage="done",
                      usage=result.get("usage"), seconds=round(time.time() - started, 2))

            # 5. only now does this turn enter the conversation window. A
            #    question that produced no answer should not be remembered.
            events.push_turn(session_id, models.ROLE_USER, question)
            events.push_turn(session_id, models.ROLE_ASSISTANT, content)

            log.info("answered %s in %.1fs (%d sources)",
                     message_id, time.time() - started, len(sources))
        except Exception as exc:                 # noqa: BLE001
            db.rollback()
            row = db.get(models.Message, message_id)
            # Two guards, both defense in depth now that push_turn() no
            # longer raises on a Redis blip (shared/events.py): `row` can be
            # None if the message was deleted out from under this handler,
            # and — the case that mattered here — this whole `try` can raise
            # *after* the answer was already committed as MESSAGE_DONE a few
            # lines up, in which case overwriting it with a generic failure
            # would clobber a correct answer the caller cannot see was ever
            # generated.
            if row is not None and row.status != models.MESSAGE_DONE:
                row.status = models.MESSAGE_FAILED
                row.content = f"Something went wrong while answering: {exc}"
                db.commit()
                _announce(session_id, row, stage="failed")
            raise                                 # let the queue retry it


# --- the pieces --------------------------------------------------------------

def build_prompt(question: str, sources: list[dict], history: list[dict]) -> str:
    """Assemble one string for the model.

    Unchanged from version 2, on purpose: this is where answer quality
    actually lives, and it was never the thing that was broken.
    """
    parts = [SYSTEM_INSTRUCTIONS.strip()]

    if sources:
        lines = ["Context:"]
        for number, source in enumerate(sources, start=1):
            label = source.get("source") or "unknown"
            if source.get("section"):
                label = f"{label} - {source['section']}"
            lines.append(f"[{number}] ({label})\n{source.get('text', '')}")
        parts.append("\n\n".join(lines))
    else:
        parts.append("Context: nothing relevant was found in the documents.")

    if history:
        turns = [
            f"{'User' if t['role'] == models.ROLE_USER else 'Assistant'}: {t['content']}"
            for t in history
        ]
        parts.append("Recent conversation:\n" + "\n".join(turns))

    parts.append(f"User: {question}\nAssistant:")
    return "\n\n".join(parts)


def publish_token(session_id: str, message_id: str, text: str) -> None:
    """Send a fragment of an answer to whoever is watching.

    Called once today, because the model service returns a finished string.
    When it can stream, this is called in a loop and nothing else changes.
    """
    events.publish(session_id, {
        "event": events.MESSAGE_TOKEN,
        "message_id": message_id,
        "text": text,
    })


def _announce(session_id: str, row, **extra) -> None:
    events.publish(session_id, {
        "event": events.MESSAGE_STATUS,
        "message_id": row.id,
        "status": row.status,
        **extra,
    })


def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    init_db()
    # A shorter heartbeat than ingest: a question should never take minutes, so
    # if one does we want the message back in the queue sooner.
    Worker("chat", settings.chat_queue_url, handle, heartbeat_seconds=30).run()


if __name__ == "__main__":
    main()
