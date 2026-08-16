"""Calls from this app back to the OpenHost router, authenticated with our app token."""

from typing import Any

import httpx
from loguru import logger

from server.core.config import Config

_TIMEOUT_SECS = 10.0


class RouterCallFailed(Exception):
    pass


async def grant_app_scoped(config: Config, consumer_app_id: str, service_url: str, grant: dict[str, Any]) -> None:
    """Register a permission this app is granting to ``consumer_app_id`` for one of its own services.

    The router takes the granting provider from the bearer token, so this can only ever create
    grants against md-notes itself.
    """
    if not config.router_url or not config.app_token:
        raise RouterCallFailed("router URL / app token not configured — not running on OpenHost")
    url = f"{config.router_url.rstrip('/')}/api/permissions/v2/grant_app_scoped"
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECS) as client:
        response = await client.post(
            url,
            json={"consumer_app_id": consumer_app_id, "service_url": service_url, "grant": grant},
            headers={"Authorization": f"Bearer {config.app_token}"},
        )
    if response.status_code != 200:
        logger.error("grant_app_scoped failed: {} {}", response.status_code, response.text[:300])
        raise RouterCallFailed(f"router returned {response.status_code}")
    logger.info("Granted {} access to {}", consumer_app_id, grant)
