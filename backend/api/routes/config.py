"""Where the three model services are.

Unchanged in purpose from version 2: they live on a Colab runtime behind a
tunnel whose address changes every restart, so the addresses are set at
runtime and kept in Redis rather than baked into a config file.

What changed: version 2 left both endpoints wide open -- anyone who could
reach the API could repoint where the embedding/STT/LLM traffic goes. Only
the *write* half needed Cognito's "admins" group to close that gap: reading
where the services are (and whether they are healthy) is what every signed-in
user's own connect screen needs before it can let them upload anything, so
`GET` stays `require_user` and only `PUT` is `require_admin`. Gating the read
too — an earlier pass through this file did, briefly — leaves ordinary users
stuck on the connect screen forever, unable to learn that the services are
already configured and ready.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from shared import clients, services
from shared.auth import require_admin, require_user
from shared.config import get_settings
from shared.schemas import ServiceConfigOut, ServiceUrls
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/config", tags=["config"])
log = logging.getLogger(__name__)
settings = get_settings()


async def _probe(urls: dict[str, str]) -> dict[str, dict | None]:
    """Ask all three at once. Sequentially this takes thirty seconds when down."""
    results = await asyncio.gather(*[
        run_in_threadpool(clients.health, url) for url in urls.values()
    ])
    return dict(zip(urls.keys(), results))


@router.get("/services", response_model=ServiceConfigOut)
async def read_services(_user: str = Depends(require_user)) -> ServiceConfigOut:
    urls = await run_in_threadpool(services.read_urls)
    probed = await _probe(urls)
    healthy = {name: body is not None for name, body in probed.items()}
    return ServiceConfigOut(urls=urls, healthy=healthy, ready=all(healthy.values()))


@router.put("/services", response_model=ServiceConfigOut)
async def write_services(body: ServiceUrls,
                         _admin: str = Depends(require_admin)) -> ServiceConfigOut:
    """Point the system at three new addresses.

    Checks that each one answers *and* that it is the service it is supposed
    to be, because all three reply to /health and three addresses pasted in
    the wrong order otherwise look perfectly healthy until an upload fails
    later.
    """
    candidate = {
        "embedding": body.embedding.strip().rstrip("/"),
        "stt": body.stt.strip().rstrip("/"),
        "llm": body.llm.strip().rstrip("/"),
    }

    for name, url in candidate.items():
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400,
                                detail=f"{name}: address must start with http:// or https://")

    probed = await _probe(candidate)

    unreachable = [n for n, body_ in probed.items() if body_ is None]
    if unreachable:
        raise HTTPException(
            status_code=400,
            detail=f"could not reach: {', '.join(unreachable)}. "
                   "Check the tunnel is still open and the URL was copied whole.",
        )

    swapped = [
        f"{name} address is actually the {body_.get('service') or 'unknown'} service"
        for name, body_ in probed.items()
        if body_.get("service") and body_["service"] != name
    ]
    if swapped:
        raise HTTPException(status_code=400,
                            detail="; ".join(swapped) + ". Check the order you pasted them in.")

    await run_in_threadpool(services.write_urls, candidate)
    healthy = {name: True for name in services.NAMES}
    return ServiceConfigOut(urls=candidate, healthy=healthy, ready=True)
