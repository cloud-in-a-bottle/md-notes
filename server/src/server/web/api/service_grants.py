"""Owner-facing half of the notes-service consent flow, backing the ``/service/grant`` page.

The consumer app never talks to these routes — it only hands the owner a link. The page reads the
pending request, the owner picks a vault and files, and this app registers the resulting grant with
the router under its own app identity.
"""

from litestar import Controller
from litestar import get
from litestar import post
from litestar.exceptions import ClientException
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.params import FromQuery

from server.core.config import Config
from server.core.db import get_service_grant_request
from server.core.files import file_exists
from server.core.router_api import grant_app_scoped
from server.core.service_grants import SERVICE_URL
from server.core.service_grants import build_grant
from server.core.service_grants import validated_return_to
from server.core.vaults import vault_root
from server.models.common import OkResponse
from server.models.service import ApproveGrantBody
from server.models.service import GrantRequestInfo


class ServiceGrantsController(Controller):
    path = "/api/service-grants"

    @get("/request/{token:str}")
    async def read_request(
        self, token: FromPath[str], config: Config, return_to: FromQuery[str | None] = None
    ) -> GrantRequestInfo:
        request = get_service_grant_request(token)
        if request is None:
            raise NotFoundException(detail="This request link is unknown or has expired")
        return GrantRequestInfo(
            consumerName=request.consumer_name,
            access=request.access,
            returnTo=validated_return_to(config, return_to),
        )

    @post("/approve", status_code=200)
    async def approve(self, data: ApproveGrantBody, config: Config) -> OkResponse:
        request = get_service_grant_request(data.token)
        if request is None:
            raise NotFoundException(detail="This request link is unknown or has expired")
        root = vault_root(config.vault_path, data.vault)  # raises VaultNotFound → 404
        if data.paths is not None:
            if not data.paths:
                raise ClientException(detail="select at least one file, or grant the whole vault")
            missing = [p for p in data.paths if not file_exists(root, p)]
            if missing:
                raise ClientException(detail=f"no such file(s): {', '.join(missing)}")
        grant = build_grant(data.vault, data.paths, request.access)
        await grant_app_scoped(config, request.consumer_app_id, SERVICE_URL, grant.to_payload())
        return OkResponse()
