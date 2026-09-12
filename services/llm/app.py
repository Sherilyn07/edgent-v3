"""The LLM service — port 8003.

One endpoint. A prompt goes in, an answer comes out. It holds no state, talks
to no database, and makes no decisions: everything it needs is in the request.

That is what makes it the easiest piece of the whole system to replace later.

Run it from the `backend/services` folder:

    uvicorn llm.app:app --reload --port 8003

Take the --reload off once it works. Reloading makes it drop and reload two
gigabytes of weights every time you touch a file.
"""
import logging

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import model
from .config import get_settings, resolve_device, resolve_dtype
from .schemas import GenerateRequest, GenerateResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("llm")
settings = get_settings()

app = FastAPI(title="EdgentRAG v1 — LLM Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter()


@router.get("/health")
def health() -> dict:
    """PROVIDED — working.

    Reports the resolved device and dtype rather than the literal "auto", so a
    glance at this tells you whether the GPU is really being used.
    """
    device = resolve_device(settings.device)
    return {"status": "ok", "service": "llm", "model": settings.llm_model,
            "device": device, "dtype": resolve_dtype(settings.dtype, device)}


@router.post("/generate", response_model=GenerateResponse)
def generate_endpoint(body: GenerateRequest) -> GenerateResponse:
    """Answer a prompt.

    STUDENT WORK — and it is short, because model.py does the work:

      1. result = model.generate(body.prompt, body.max_new_tokens,
                                 body.temperature)
      2. Log result["usage"] so the numbers show up in your terminal.
      3. Return GenerateResponse(**result).

    One request at a time, on purpose. FastAPI will happily accept a second
    request while the first is still generating, and both will crawl. Try it
    once, watch it happen, and you will understand why serving models properly
    is its own discipline.
    """
    try:
        result = model.generate(body.prompt, body.max_new_tokens, body.temperature)
        log.info("usage: %s", result["usage"])
        return GenerateResponse(**result)
    except Exception as exc:
        log.exception("generate failed")
        raise HTTPException(status_code=500, detail=f"generation failed: {exc}") from exc


app.include_router(router)
