"""The contract with the monolith.

CHANGED FOR COLAB. The service used to be handed a storage *key* and go and
read the video off the monolith's disk. It cannot do that from Colab, so the
monolith now uploads the bytes and gets the chunks back inline. The monolith
writes the transcript and the chunks file into its own storage.

The request is multipart/form-data (see app.py), so there is no request model
here -- only what comes back.
"""
from pydantic import BaseModel


class TranscribeResponse(BaseModel):
    chunks: list[dict]      # the chunk records, ready to be written as .jsonl
    chunk_count: int
    seconds: float          # how long the audio was, after any truncation
    transcript: str         # the plain readable transcript, for a human to read
