"""A minimal OpenHost app that consumes md-notes' notes service.

It exists to exercise the service from the outside — the permission handoff and the four read
endpoints — both from a browser (the pages below) and from tests (``POST /call-service``).

Stdlib only, so the image builds without a network.

Routes:
    GET  /health        -> {"status": "ok"}
    GET  /              -> lists the notes it can read; on 403, links to md-notes' consent page
    GET  /read          -> ?vault=&path=[&header=] renders one file or section as text
    POST /call-service  -> {"path", "query"?, "method"?} proxied through the router;
                           returns {"status", "body", "grant_url"}
"""

import html
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from typing import Any

SHORTNAME = "notes"


def own_origin() -> str:
    """This app's public URL — where md-notes should send the owner back to."""
    zone = os.environ["OPENHOST_ZONE_DOMAIN"]
    scheme = "http" if "localhost" in zone else "https"
    return f"{scheme}://{os.environ['OPENHOST_APP_NAME']}.{zone}"


def call_service(path: str, query: dict[str, str] | None = None, method: str = "GET") -> tuple[int, Any]:
    """Call the notes service through the router, with our app identity attached."""
    url = f"{os.environ['OPENHOST_ROUTER_URL']}/api/services/v2/call/{SHORTNAME}/{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {os.environ['OPENHOST_APP_TOKEN']}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        return 502, {"error": "router unreachable from app container", "detail": str(e)}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def grant_url_from(body: Any, return_to: str) -> str | None:
    """The consent-page URL out of a permission_required body, with our return address attached."""
    if not isinstance(body, dict) or body.get("error") != "permission_required":
        return None
    url = body.get("grant_url")
    if not isinstance(url, str):
        return None
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urllib.parse.urlencode({'return_to': return_to})}"


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>notes-reader</title></head>
<body><h1>notes-reader</h1>{body}</body></html>
"""


def index_page() -> str:
    status, body = call_service("files")
    if status == 200:
        files = body.get("files", []) if isinstance(body, dict) else []
        if not files:
            return _PAGE.format(body='<p id="status">No notes shared with me.</p>')
        items = "".join(
            '<li class="file"><a href="/read?{query}">{label}</a></li>'.format(
                query=html.escape(urllib.parse.urlencode({"vault": f["vault"], "path": f["path"]})),
                label=html.escape(f"{f['vault']}/{f['path']}"),
            )
            for f in files
        )
        return _PAGE.format(body=f'<p id="status">Can read {len(files)} note(s).</p><ul id="files">{items}</ul>')

    grant_url = grant_url_from(body, f"{own_origin()}/")
    if grant_url:
        return _PAGE.format(
            body='<p id="status">No access yet.</p>'
            f'<p><a id="grant-link" href="{html.escape(grant_url)}">Grant notes-reader access to your notes</a></p>'
        )
    return _PAGE.format(body=f'<p id="status">Service call failed ({status}).</p>')


def read_page(query: dict[str, str]) -> str:
    endpoint = "file-section" if query.get("header") else "file"
    status, body = call_service(endpoint, query)
    if status != 200:
        return _PAGE.format(body=f'<p id="status">Service call failed ({status}).</p>')
    return _PAGE.format(body=f'<pre id="content">{html.escape(str(body))}</pre>')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    def _send(self, status: int, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, body: dict[str, Any]) -> None:
        self._send(status, "application/json", json.dumps(body).encode())

    def _html(self, markup: str) -> None:
        self._send(200, "text/html; charset=utf-8", markup.encode())

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if parsed.path == "/health":
            self._json(200, {"status": "ok"})
        elif parsed.path == "/":
            self._html(index_page())
        elif parsed.path == "/read":
            self._html(read_page(query))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/call-service":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        status, service_body = call_service(body["path"], body.get("query"), body.get("method", "GET"))
        self._json(
            200,
            {
                "status": status,
                "body": service_body,
                "grant_url": grant_url_from(service_body, body.get("return_to") or f"{own_origin()}/"),
            },
        )


if __name__ == "__main__":
    print("notes-reader listening on :5000", flush=True)
    HTTPServer(("0.0.0.0", 5000), Handler).serve_forever()
