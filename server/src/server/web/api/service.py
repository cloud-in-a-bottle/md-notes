"""The notes service: read-only vault access for other OpenHost apps (``services/notes/openapi.yaml``).

Requests arrive through the router's v2 service proxy, which is the only source of the
``X-OpenHost-*`` headers these routes trust — it strips any the caller tried to set. Authorization
is entirely by the grants in ``X-OpenHost-Permissions``; a call the grants don't cover gets a 403
carrying a link to this app's consent page (see ``service_grants.py``).

Content comes out of the live Y.Doc, so in-flight edits are visible, and it is the plain markdown
body — comments and other md-notes metadata live elsewhere in the doc and never appear here.
"""

from typing import Any

from litestar import Controller
from litestar import Request
from litestar import get
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.exceptions import NotFoundException
from litestar.handlers import BaseRouteHandler
from litestar.params import FromQuery

from server.core.config import Config
from server.core.files import file_exists
from server.core.files import list_files
from server.core.markdown_headers import extract_section
from server.core.markdown_headers import parse_headers
from server.core.service_grants import Access
from server.core.service_grants import NotesGrant
from server.core.service_grants import allows
from server.core.service_grants import granted_vaults
from server.core.service_grants import parse_permissions_header
from server.core.sync import SyncManager
from server.core.vaults import VaultNotFound
from server.core.vaults import vault_root
from server.models.files import FileEntry
from server.models.service import ServiceFileList
from server.models.service import ServiceFileRef
from server.models.service import ServiceHeader
from server.models.service import ServiceHeaderList

MARKDOWN = "text/markdown"


class ServicePermissionRequired(Exception):
    """Raised when the caller's grants don't cover the request; turned into the documented 403."""

    def __init__(self, access: Access = "read") -> None:
        super().__init__(access)
        self.access = access


def requires_service_caller(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # type: ignore[type-arg]
    """Only accept requests the router proxied on some app's behalf."""
    if not connection.headers.get("x-openhost-consumer-id"):
        raise NotAuthorizedException(detail="Not a service call")


def _grants(connection: ASGIConnection) -> tuple[NotesGrant, ...]:  # type: ignore[type-arg]
    return parse_permissions_header(connection.headers.get("x-openhost-permissions"))


def _file_paths(entries: list[FileEntry]) -> list[str]:
    paths: list[str] = []
    for entry in entries:
        if entry.type == "dir":
            paths.extend(_file_paths(entry.children or []))
        else:
            paths.append(entry.path)
    return paths


class NotesServiceController(Controller):
    path = "/api/service/notes"
    # Opts out of the app-wide owner guard: these callers are apps, not the owner.
    opt = {"public": True}
    guards = [requires_service_caller]

    @get("/files")
    async def list_readable(
        self, request: Request[Any, Any, Any], config: Config, vault: FromQuery[str | None] = None
    ) -> ServiceFileList:
        grants = _grants(request)
        vaults = granted_vaults(grants, "read")
        if not vaults:
            raise ServicePermissionRequired("read")
        if vault is not None:
            if vault not in vaults:
                raise ServicePermissionRequired("read")
            vaults = [vault]

        files: list[ServiceFileRef] = []
        for name in vaults:
            try:
                root = vault_root(config.vault_path, name)
            except VaultNotFound:
                continue  # a grant outlives the vault it named
            files.extend(
                ServiceFileRef(vault=name, path=path)
                for path in _file_paths(list_files(root))
                if allows(grants, name, path, "read")
            )
        return ServiceFileList(files=files)

    @get("/file-headers")
    async def list_file_headers(
        self, request: Request[Any, Any, Any], config: Config, vault: FromQuery[str], path: FromQuery[str]
    ) -> ServiceHeaderList:
        content = await self._read_markdown(request, config, vault, path)
        headers = [ServiceHeader(level=h.level, text=h.text, slug=h.slug, line=h.line) for h in parse_headers(content)]
        return ServiceHeaderList(vault=vault, path=path, headers=headers)

    @get("/file-section", media_type=MARKDOWN)
    async def read_file_section(
        self,
        request: Request[Any, Any, Any],
        config: Config,
        vault: FromQuery[str],
        path: FromQuery[str],
        header: FromQuery[str],
    ) -> str:
        section = extract_section(await self._read_markdown(request, config, vault, path), header)
        if section is None:
            raise NotFoundException(detail=f"No header {header!r} in {path}")
        return section

    @get("/file", media_type=MARKDOWN)
    async def read_file(
        self, request: Request[Any, Any, Any], config: Config, vault: FromQuery[str], path: FromQuery[str]
    ) -> str:
        return await self._read_markdown(request, config, vault, path)

    @staticmethod
    async def _read_markdown(request: Request[Any, Any, Any], config: Config, vault: str, path: str) -> str:
        if not allows(_grants(request), vault, path, "read"):
            raise ServicePermissionRequired("read")
        # Also rejects paths that climb out of the vault, which a whole-vault grant would let past.
        if not file_exists(vault_root(config.vault_path, vault), path):
            raise NotFoundException(detail=f"No file {path!r} in vault {vault!r}")
        manager: SyncManager = request.app.state.sync_manager
        return await manager.read_doc(f"{vault}/{path.lstrip('/')}")
