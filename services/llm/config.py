"""Settings for the LLM service.

Runs on port 8003. Holds no state and touches no storage -- everything it needs
arrives in the request, which is what makes it the easiest piece of the system
to move onto a GPU.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # TinyLlama: 1.1 billion parameters, about 2.2 GB, and it will run on a
    # laptop CPU. It is not clever.
    #
    # On an A100 80GB this is a waste of the card -- the same code runs
    # meta-llama/Llama-3.1-8B-Instruct (needs a HF token and licence acceptance)
    # or Qwen/Qwen2.5-7B-Instruct (no gate) by changing this one line. Bump
    # MAX_INPUT_TOKENS at the same time; those models have far more context.
    llm_model: str = "TinyLlama/TinyLlama-1.1B-Chat-v1.0"

    # "auto" -> cuda when a GPU is present, cpu otherwise.
    device: str = "auto"
    # "auto" -> bfloat16 on a GPU, float32 on a CPU.
    dtype: str = "auto"

    max_new_tokens: int = 400
    temperature: float = 0.3
    max_input_tokens: int = 1600      # TinyLlama's context is 2048 -- leave room to answer


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


def resolve_dtype(requested: str, device: str) -> str:
    if requested != "auto":
        return requested
    return "bfloat16" if device == "cuda" else "float32"
