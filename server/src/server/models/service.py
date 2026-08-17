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
class GrantRequestInfo:
    """The consent page's view of a request, after the server has vetted the link's parameters."""

    consumerName: str
    access: Access
    # The consumer-supplied return URL, echoed back only if it points into this OpenHost space.
    returnTo: str | None


@attr.s(auto_attribs=True, frozen=True)
class ApproveGrantBody:
    # Name of the app being granted access. Also what the router keys the grant to, so the page
    # can't show one app and grant another.
    consumer: str
    access: Access
    vault: str
    # None grants the whole vault, including files added later.
    paths: list[str] | None = None
