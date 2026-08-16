"""Permission grants for the notes service (see ``services/notes/openapi.yaml``).

A grant is a payload the owner shapes in this app's consent page; the router stores it against
(consumer app, service url) and replays it on every call in ``X-OpenHost-Permissions``:

    {"vault": "personal", "paths": "*", "access": "read"}

``paths`` is ``"*"`` (the whole vault, including files added later) or a list of exact file paths.
``access`` is a tier: only ``read`` is reachable today, but ``comment`` and ``write`` are already
ordered and matched so the service can grow write endpoints without a new grant shape.

Only app-scoped grants count. Every legitimate grant names a vault inside *this* instance, so a
global-scoped grant — which would apply to any provider of this service — is never sufficient.
"""

import json
from typing import Any
from typing import Literal
from typing import cast
from urllib.parse import urlencode
from urllib.parse import urlparse

import attr

from server.core.config import Config

# Identity of the service we provide. Must match [[services.v2.provides]] in openhost.toml — the
# router keys stored grants by it.
SERVICE_URL = "github.com/imbue-openhost/md-notes/services/notes"

Access = Literal["read", "comment", "write"]

ACCESS_TIERS: tuple[Access, ...] = ("read", "comment", "write")
_ACCESS_RANK: dict[str, int] = {tier: rank for rank, tier in enumerate(ACCESS_TIERS)}

ALL_PATHS = "*"


@attr.s(auto_attribs=True, frozen=True)
class NotesGrant:
    vault: str
    # None means the whole vault, including files added after the grant.
    paths: tuple[str, ...] | None
    access: Access

    def covers(self, vault: str, path: str | None, access: Access) -> bool:
        """Whether this grant allows ``access`` on ``path`` in ``vault`` (path None = the vault itself)."""
        if self.vault != vault or _ACCESS_RANK[self.access] < _ACCESS_RANK[access]:
            return False
        return path is None or self.paths is None or path in self.paths

    def to_payload(self) -> dict[str, Any]:
        return {
            "vault": self.vault,
            "paths": ALL_PATHS if self.paths is None else list(self.paths),
            "access": self.access,
        }


def build_grant(vault: str, paths: list[str] | None, access: Access) -> NotesGrant:
    """Normalise a grant the consent page produced: deduplicated, ordered, no leading slashes."""
    if paths is None:
        return NotesGrant(vault=vault, paths=None, access=access)
    return NotesGrant(
        vault=vault, paths=tuple(sorted({p.strip().lstrip("/") for p in paths if p.strip()})), access=access
    )


def parse_grant(entry: Any) -> NotesGrant | None:
    """Decode one ``X-OpenHost-Permissions`` entry, or None if it isn't a usable notes grant."""
    if not isinstance(entry, dict) or entry.get("scope") != "app":
        return None
    grant = entry.get("grant")
    if not isinstance(grant, dict):
        return None
    vault = grant.get("vault")
    access = grant.get("access")
    raw_paths = grant.get("paths", ALL_PATHS)
    if not isinstance(vault, str) or not vault or access not in _ACCESS_RANK:
        return None
    if raw_paths == ALL_PATHS:
        paths = None
    elif isinstance(raw_paths, list) and all(isinstance(p, str) for p in raw_paths):
        paths = tuple(raw_paths)
    else:
        return None
    return NotesGrant(vault=vault, paths=paths, access=cast(Access, access))


def parse_permissions_header(header: str | None) -> tuple[NotesGrant, ...]:
    """Every notes grant in the router's ``X-OpenHost-Permissions`` header. Junk entries are dropped."""
    if not header:
        return ()
    try:
        entries = json.loads(header)
    except ValueError:
        return ()
    if not isinstance(entries, list):
        return ()
    return tuple(g for g in (parse_grant(e) for e in entries) if g is not None)


def allows(grants: tuple[NotesGrant, ...], vault: str, path: str | None, access: Access) -> bool:
    return any(g.covers(vault, path, access) for g in grants)


def grant_page_url(config: Config, token: str) -> str:
    """Where we send a consumer's user to shape a grant. The token carries the requester's identity,
    so nothing about who is asking rides in the link itself."""
    if not config.app_origin:
        raise RuntimeError("app_origin not configured — cannot build grant URLs")
    return f"{config.app_origin}/service/grant?{urlencode({'request': token})}"


def validated_return_to(config: Config, url: str | None) -> str | None:
    """``url`` if it points at an app in this OpenHost space, else None.

    The consent page bounces the browser here when it's done, and the value comes from whoever
    built the link — so anything outside the space is dropped rather than becoming an open redirect.
    """
    if not url or not config.zone_domain:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    zone_host = config.zone_domain.split(":")[0].lower()
    host = parsed.hostname.lower()
    return url if host == zone_host or host.endswith(f".{zone_host}") else None


def granted_vaults(grants: tuple[NotesGrant, ...], access: Access) -> list[str]:
    """Vaults with at least one grant at ``access`` or above, in first-granted order."""
    seen: list[str] = []
    for grant in grants:
        if _ACCESS_RANK[grant.access] >= _ACCESS_RANK[access] and grant.vault not in seen:
            seen.append(grant.vault)
    return seen
