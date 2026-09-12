"""Settings for the speech-to-text service.

Runs on port 8002. No storage settings: the monolith uploads the media and
takes the chunks back, so this service never touches its disk.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "tiny.en" is the smallest Whisper there is: about 75 MB and English only.
    # On an A100 there is no reason to stay here -- "large-v3" is a different
    # class of accuracy and still runs faster than real time. One line.
    whisper_model: str = "tiny.en"

    # "auto" -> cuda when a GPU is present, cpu otherwise.
    device: str = "auto"
    # "auto" picks float16 on a GPU and int8 on a CPU.
    compute_type: str = "auto"

    max_seconds: int = 1800           # 30 minutes; anything longer is cut off

    # How long we will spend pulling a video out of storage before giving up.
    # Generous: this is a large file over the public internet.
    download_timeout_seconds: int = 900

    # Chunking, kept the same as the monolith's so a video and a PDF produce
    # comparable pieces.
    chunk_words: int = 200
    chunk_overlap_words: int = 40

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
    if requested != "auto":
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def resolve_compute_type(requested: str, device: str) -> str:
    if requested != "auto":
        return requested
    return "float16" if device == "cuda" else "int8"
