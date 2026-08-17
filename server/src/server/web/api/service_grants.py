"""Owner-facing half of the notes-service consent flow, backing the ``/service/grant`` page.

The consumer app never talks to these routes — it only hands the owner a link naming itself. That
name is passed straight through to the router when the grant is made, so the app the page describes
is the app that gets access and there is nothing here to protect against tampering. These routes
just vet the link's parameters and register what the owner chose.
"""

from litestar import Controller
from litestar import get
from litestar import post
from litestar.exceptions import ClientException
from litestar.params import FromQuery

from server.core.config import Config
from server.core.files import file_exists
from server.core.router_api import grant_app_scoped
from server.core.service_grants import SERVICE_URL
from server.core.service_grants import build_grant
from server.core.service_grants import parse_access
from server.core.service_grants import validated_return_to
from server.core.vaults import vault_root
from server.models.common import OkResponse
from server.models.service import ApproveGrantBody
from server.models.service import GrantRequestInfo


class ServiceGrantsController(Controller):
    path = "/api/service-grants"

    @get("/request")
    async def read_request(
        self,
        config: Config,
        consumer: FromQuery[str],
        access: FromQuery[str],
        return_to: FromQuery[str | None] = None,
    ) -> GrantRequestInfo:
        tier = parse_access(access)
        if not consumer.strip() or tier is None:
            raise ClientException(detail="this grant link is malformed")
        return GrantRequestInfo(
            consumerName=consumer,
            access=tier,
            returnTo=validated_return_to(config, return_to),
        )

    @post("/approve", status_code=200)
    async def approve(self, data: ApproveGrantBody, config: Config) -> OkResponse:
        tier = parse_access(data.access)
        if not data.consumer.strip() or tier is None:
            raise ClientException(detail="this grant link is malformed")
        root = vault_root(config.vault_path, data.vault)  # raises VaultNotFound → 404
        if data.paths is not None:
            if not data.paths:
                raise ClientException(detail="select at least one file, or grant the whole vault")
            missing = [p for p in data.paths if not file_exists(root, p)]
            if missing:
                raise ClientException(detail=f"no such file(s): {', '.join(missing)}")
        grant = build_grant(data.vault, data.paths, tier)
        # A name no app answers to gets a 404 from the router, surfaced as a 502 here.
        await grant_app_scoped(config, data.consumer, SERVICE_URL, grant.to_payload())
        return OkResponse()
