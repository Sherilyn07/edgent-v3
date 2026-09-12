"""Where the three model services currently are.

They run on a Colab runtime behind a tunnel whose address changes every restart,
so this cannot be a setting baked into an image. It is set at runtime from the
app's first screen.

Version 1 kept it in a JSON file next to the database, which worked because
there was one process. Here there are many API tasks and many workers, so it
lives in Redis: a worker picking up a message reads the same value the browser
set a moment ago, with no deploy and no restart.

The environment variables remain the defaults, which is what makes
`docker compose up` work with services on localhost and no configuration at all.
"""
import json
import logging

from .config import get_settings
from .events import sync_client

log = logging.getLogger(__name__)
settings = get_settings()

REDIS_KEY = "config:services"
NAMES = ("embedding", "stt", "llm")


def defaults() -> dict[str, str]:
    return {
        "embedding": settings.embedding_service_url,
        "stt": settings.stt_service_url,
        "llm": settings.llm_service_url,
    }


def read_urls() -> dict[str, str]:
    """The current addresses, falling back to the environment."""
    urls = defaults()
    try:
        saved = sync_client.get(REDIS_KEY)
        if saved:
            for name, value in json.loads(saved).items():
                if name in urls and isinstance(value, str) and value.strip():
                    urls[name] = value.strip().rstrip("/")
    except Exception:                            # noqa: BLE001
        log.warning("could not read service urls from redis; using defaults", exc_info=True)
    return urls


def url(name: str) -> str:
    """One address, looked up per call.

    Per call on purpose: reconnecting to a new Colab runtime then takes effect
    immediately, with nothing cached and nothing restarted.
    """
    return read_urls()[name]


def write_urls(urls: dict[str, str]) -> dict[str, str]:
    """Replace the addresses. Returns what was stored."""
    current = read_urls()
    for name in NAMES:
        value = urls.get(name)
        if isinstance(value, str) and value.strip():
            current[name] = value.strip().rstrip("/")
    sync_client.set(REDIS_KEY, json.dumps(current))
    log.info("service urls set: %s", current)
    return current
