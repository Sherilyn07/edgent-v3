"""Settings for the embedding service.

Runs on port 8001.

No storage settings any more: since the services moved off the monolith's
machine they never touch its disk. The monolith sends chunk text directly.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # A small model, on purpose. 22 million parameters, 384 numbers per chunk.
    # On an A100 you can move up to e.g. BAAI/bge-large-en-v1.5 by changing this
    # one line -- but then you must re-index, because vectors from two different
    # models are not comparable.
    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # "auto" picks cuda when a GPU is present, cpu otherwise. Set DEVICE=cuda or
    # DEVICE=cpu in .env to force it.
    device: str = "auto"
    batch_size: int = 64                # 32 is plenty on CPU; a GPU likes more
    max_length: int = 256               # this model's limit, in tokens

    # Where Chroma keeps the vectors. On Colab, point this at a Drive folder if
    # you want the index to survive the runtime being recycled.
    chroma_dir: str = "./_chroma"

    # --- the broker ----------------------------------------------------------
    # Where the backend is, and the one token we hold. Set both and this
    # service pulls jobs; leave them blank and it only answers HTTP calls,
    # which is what you want when testing it on its own.
    #
    # The token grants "ask for a job" and nothing else. It is not an AWS
    # credential and cannot be turned into one.
    broker_url: str = ""
    broker_token: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def resolve_device(requested: str) -> str:
    """Turn "auto" into a real device name."""
    if requested != "auto":
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"
