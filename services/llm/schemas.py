"""The contract with the monolith. PROVIDED — do not change."""
from pydantic import BaseModel


class GenerateRequest(BaseModel):
    prompt: str
    max_new_tokens: int = 400
    temperature: float = 0.3


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    tokens_per_second: float


class GenerateResponse(BaseModel):
    content: str
    usage: Usage
