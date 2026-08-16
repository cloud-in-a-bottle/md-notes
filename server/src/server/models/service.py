import attr

from server.core.service_grants import Access


@attr.s(auto_attribs=True, frozen=True)
class ServiceFileRef:
    vault: str
    path: str


@attr.s(auto_attribs=True, frozen=True)
class ServiceFileList:
    files: list[ServiceFileRef]


@attr.s(auto_attribs=True, frozen=True)
class ServiceHeader:
    level: int
    text: str
    slug: str
    line: int


@attr.s(auto_attribs=True, frozen=True)
class ServiceHeaderList:
    vault: str
    path: str
    headers: list[ServiceHeader]


@attr.s(auto_attribs=True, frozen=True)
class ServiceGrantRequest:
    """A pending consent handoff: recorded when we 403 a consumer, spent on the consent page.

    The consumer's identity is captured here from the router's headers rather than passed through
    the grant URL, so nothing the owner sees on the consent page (or grants) can be forged by
    tampering with the link.
    """

    token: str
    consumer_app_id: str
    consumer_name: str
    access: Access
    created_at: str


@attr.s(auto_attribs=True, frozen=True)
class GrantRequestInfo:
    """What the consent page needs to render — the consumer's app id is deliberately not exposed."""

    consumerName: str
    access: Access
    # The consumer-supplied return URL, echoed back only if it points into this OpenHost space.
    returnTo: str | None


@attr.s(auto_attribs=True, frozen=True)
class ApproveGrantBody:
    token: str
    vault: str
    # None grants the whole vault, including files added later.
    paths: list[str] | None = None
