"""End-to-end tests for the notes service: a real consumer app, the real router, the real grant flow.

``tests/consumer_app`` is deployed alongside md-notes and declares it consumes the notes service.
Everything here goes through the router's v2 service proxy, so the permission headers, the 403
handoff, and the app-scoped grant the consent page registers are all the production ones.
"""

import time
from pathlib import Path
from typing import Any

import pytest
import requests
from openhost_test_harness import OpenhostStack
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.containers

CONSUMER_DIR = Path(__file__).parent / "consumer_app"
CONSUMER_NAME = "notes-reader"
SERVICE_URL = "github.com/cloud-in-a-bottle/md-notes/services/notes"

VAULT = "svcvault"
SHARED = "shared.md"
PRIVATE = "private.md"
# Edited by the live-CRDT test, so it can't disturb the fixed content the other tests assert on.
LIVE = "live.md"
SHARED_CONTENT = """\
# Shared note

Intro.

## Plan

Ship it.

### Later

Maybe.

## Other

Fin.
"""


# Module fixtures here must be idempotent: pytest re-runs them whenever browser parametrization
# splits this module's tests into non-contiguous groups, and they share one session-scoped stack.


@pytest.fixture(scope="module", autouse=True)
def _seed_vault(stack: OpenhostStack) -> None:
    s = stack.owner_session
    s.post(f"{stack.url}/api/vaults", json={"name": VAULT})
    seeded = ((SHARED, SHARED_CONTENT), (PRIVATE, "# Private\n\nSecret."), (LIVE, "# Live\n\nBaseline."))
    for path, content in seeded:
        r = s.post(f"{stack.url}/api/docs/{VAULT}/file", params={"path": path}, json={"content": content})
        assert r.status_code in (201, 409), r.text


@pytest.fixture(scope="module")
def consumer(stack: OpenhostStack) -> str:
    """Deploy the dummy consumer app; returns its URL through the router."""
    if CONSUMER_NAME not in stack.local_stack.deployed_app_names:
        stack.deploy_app(f"file://{CONSUMER_DIR}")
    return stack.url_for(CONSUMER_NAME)


@pytest.fixture(autouse=True)
def _revoke_grants(stack: OpenhostStack, consumer: str) -> Any:
    """Each test starts from no grants, so the 403 path stays honest."""
    yield
    for permission in stack.owner_session.get(f"{stack.router_url}/api/permissions/v2").json():
        if permission["service_url"] == SERVICE_URL:
            stack.owner_session.post(
                f"{stack.router_url}/api/permissions/v2/revoke",
                json={
                    "app_id": permission["consumer_app_id"],
                    "service_url": SERVICE_URL,
                    "grant": permission["grant"],
                    "scope": permission["scope"],
                    "provider_app_id": permission["provider_app_id"],
                },
            )


def call(stack: OpenhostStack, consumer: str, endpoint: str, **query: str) -> dict[str, Any]:
    """Make the consumer app call the service; returns {status, body, grant_url}."""
    r = stack.owner_session.post(f"{consumer}/call-service", json={"path": endpoint, "query": query}, timeout=60)
    assert r.status_code == 200, f"consumer /call-service failed: {r.status_code}: {r.text[:300]}"
    return r.json()  # type: ignore[no-any-return]


def request_token(stack: OpenhostStack, consumer: str) -> str:
    """Trigger the 403 and pull the consent-request token out of the grant URL it hands back."""
    grant_url = call(stack, consumer, "files")["grant_url"]
    return grant_url.split("request=")[1].split("&")[0]


def grant_headless(stack: OpenhostStack, consumer: str, paths: list[str] | None) -> None:
    """What the consent page does, without a browser. ``paths`` None grants the whole vault."""
    r = stack.owner_session.post(
        f"{stack.url}/api/service-grants/approve",
        json={"token": request_token(stack, consumer), "vault": VAULT, "paths": paths},
    )
    assert r.status_code == 200, r.text


# ── Without a grant ─────────────────────────────────────────────────────


def test_call_without_grant_asks_for_permission(stack: OpenhostStack, consumer: str) -> None:
    result = call(stack, consumer, "files")
    assert result["status"] == 403
    assert result["body"]["error"] == "permission_required"
    assert result["body"]["required_grant"] == {"grant": {"access": "read"}, "scope": "app"}
    # The consent page lives in md-notes, and the consumer can hand the user straight to it.
    assert result["body"]["grant_url"].startswith(f"{stack.url}/service/grant?request=")
    assert "return_to=" in result["grant_url"]


def test_reads_are_refused_without_a_grant(stack: OpenhostStack, consumer: str) -> None:
    for endpoint in ("file", "file-headers", "file-section"):
        result = call(stack, consumer, endpoint, vault=VAULT, path=SHARED, header="plan")
        assert result["status"] == 403, endpoint
        assert result["body"]["error"] == "permission_required"


def test_service_routes_reject_callers_that_are_not_the_router(stack: OpenhostStack) -> None:
    """Only the router can vouch for a consumer, and it strips inbound X-OpenHost-* headers."""
    assert stack.owner_session.get(f"{stack.url}/api/service/notes/files").status_code == 401
    # Forged identity headers don't survive the router either.
    forged = stack.owner_session.get(
        f"{stack.url}/api/service/notes/files",
        headers={
            "X-OpenHost-Consumer-Id": "evil",
            "X-OpenHost-Permissions": '[{"grant": {"vault": "svcvault", '
            '"paths": "*", "access": "read"}, "scope": "app"}]',
        },
    )
    assert forged.status_code == 401
    # Straight at the container, with no headers at all.
    assert requests.get(f"{stack.app_url}/api/service/notes/files", timeout=10).status_code == 401


# ── The grant flow ──────────────────────────────────────────────────────


def test_grant_flow_through_the_browser(stack: OpenhostStack, consumer: str, page: Page) -> None:
    """The whole handoff as a user sees it: consumer page → md-notes consent page → back, working."""
    stack.playwright_login(page)
    page.goto(consumer)
    expect(page.locator("#status")).to_have_text("No access yet.")

    page.locator("#grant-link").click()
    page.wait_for_url(lambda url: "/service/grant?" in url)
    expect(page.locator(".service-grant-consumer")).to_have_text(CONSUMER_NAME)

    page.locator(f'.vault-picker-item[data-vault="{VAULT}"]').click()
    page.locator('.service-grant-scope input[value="files"]').check()
    page.locator(f'.service-grant-files input[value="{SHARED}"]').check()
    page.get_by_role("button", name="Grant access").click()

    # Back on the consumer, which retries the call and now sees exactly the granted file.
    page.wait_for_url(lambda url: url.startswith(consumer) and "granted=1" in url, timeout=15_000)
    expect(page.locator("#status")).to_have_text("Can read 1 note(s).")
    expect(page.locator("#files .file")).to_have_text([f"{VAULT}/{SHARED}"])


def test_declining_sends_the_user_back_empty_handed(stack: OpenhostStack, consumer: str, page: Page) -> None:
    grant_url = call(stack, consumer, "files")["grant_url"]
    stack.playwright_login(page)
    page.goto(grant_url)
    page.get_by_role("button", name="Don't share").click()
    page.wait_for_url(lambda url: url.startswith(consumer) and "granted=0" in url, timeout=15_000)
    expect(page.locator("#status")).to_have_text("No access yet.")


def test_consent_page_drops_a_return_url_outside_the_space(stack: OpenhostStack, consumer: str) -> None:
    token = request_token(stack, consumer)
    info = stack.owner_session.get(
        f"{stack.url}/api/service-grants/request/{token}", params={"return_to": consumer}
    ).json()
    assert info["consumerName"] == CONSUMER_NAME
    assert info["access"] == "read"
    assert info["returnTo"] == consumer

    evil = stack.owner_session.get(
        f"{stack.url}/api/service-grants/request/{token}", params={"return_to": "https://evil.example.com/"}
    ).json()
    assert evil["returnTo"] is None


def test_unknown_request_token_is_rejected(stack: OpenhostStack) -> None:
    assert stack.owner_session.get(f"{stack.url}/api/service-grants/request/nope").status_code == 404
    r = stack.owner_session.post(
        f"{stack.url}/api/service-grants/approve", json={"token": "nope", "vault": VAULT, "paths": None}
    )
    assert r.status_code == 404


def test_consent_page_needs_the_owner(stack: OpenhostStack, consumer: str) -> None:
    token = request_token(stack, consumer)
    anonymous = requests.get(f"{stack.url}/api/service-grants/request/{token}", allow_redirects=False, timeout=10)
    assert anonymous.status_code in (302, 401)


# ── Reading, once granted ───────────────────────────────────────────────


def test_whole_vault_grant_reads_everything(stack: OpenhostStack, consumer: str) -> None:
    grant_headless(stack, consumer, None)
    files = call(stack, consumer, "files")
    assert files["status"] == 200
    assert sorted(f["path"] for f in files["body"]["files"]) == sorted([LIVE, PRIVATE, SHARED])
    assert {f["vault"] for f in files["body"]["files"]} == {VAULT}

    whole = call(stack, consumer, "file", vault=VAULT, path=SHARED)
    assert whole["status"] == 200
    assert whole["body"] == SHARED_CONTENT


def test_file_grant_hides_everything_else(stack: OpenhostStack, consumer: str) -> None:
    grant_headless(stack, consumer, [SHARED])
    files = call(stack, consumer, "files")
    assert [f["path"] for f in files["body"]["files"]] == [SHARED]

    assert call(stack, consumer, "file", vault=VAULT, path=SHARED)["status"] == 200
    assert call(stack, consumer, "file", vault=VAULT, path=PRIVATE)["status"] == 403
    assert call(stack, consumer, "file-headers", vault=VAULT, path=PRIVATE)["status"] == 403


def test_a_grant_cannot_be_walked_out_of_its_vault(stack: OpenhostStack, consumer: str) -> None:
    grant_headless(stack, consumer, None)
    stack.owner_session.post(f"{stack.url}/api/vaults", json={"name": "othervault"})
    stack.owner_session.post(
        f"{stack.url}/api/docs/othervault/file", params={"path": "secret.md"}, json={"content": "nope"}
    )
    escaped = call(stack, consumer, "file", vault=VAULT, path="../othervault/secret.md")
    assert escaped["status"] == 404
    assert call(stack, consumer, "file", vault="othervault", path="secret.md")["status"] == 403


def test_list_headers(stack: OpenhostStack, consumer: str) -> None:
    grant_headless(stack, consumer, [SHARED])
    result = call(stack, consumer, "file-headers", vault=VAULT, path=SHARED)
    assert result["status"] == 200
    assert result["body"]["vault"] == VAULT
    assert result["body"]["path"] == SHARED
    assert [(h["level"], h["text"], h["slug"]) for h in result["body"]["headers"]] == [
        (1, "Shared note", "shared-note"),
        (2, "Plan", "plan"),
        (3, "Later", "later"),
        (2, "Other", "other"),
    ]


def test_read_one_header_with_its_subsections(stack: OpenhostStack, consumer: str) -> None:
    grant_headless(stack, consumer, [SHARED])
    result = call(stack, consumer, "file-section", vault=VAULT, path=SHARED, header="plan")
    assert result["status"] == 200
    assert result["body"] == "## Plan\n\nShip it.\n\n### Later\n\nMaybe."

    assert call(stack, consumer, "file-section", vault=VAULT, path=SHARED, header="nope")["status"] == 404


def test_missing_file_and_vault_are_404(stack: OpenhostStack, consumer: str) -> None:
    grant_headless(stack, consumer, None)
    assert call(stack, consumer, "file", vault=VAULT, path="ghost.md")["status"] == 404
    assert call(stack, consumer, "files", vault="ghostvault")["status"] == 403


def test_reads_reflect_unsaved_crdt_edits(stack: OpenhostStack, consumer: str, page: Page) -> None:
    """Content comes from the live Y.Doc, not the .md — which only catches up on the save debounce."""
    grant_headless(stack, consumer, None)
    marker = "LIVE_CRDT_EDIT"

    stack.playwright_login(page)
    page.goto(stack.url)
    page.locator(".vault-picker-item-name", has_text=VAULT).click()
    page.locator(f'.sidebar-item[data-type="file"][data-path="{LIVE}"]').click()
    page.wait_for_selector(".cm-editor", timeout=15_000)
    page.wait_for_timeout(1500)
    content = page.locator(".cm-content")
    content.click()
    page.keyboard.type(marker)
    expect(content).to_contain_text(marker)

    # The room's save debounce is 5s, so a service read that sees the edit sooner than that can
    # only have come from the CRDT. Confirm the .md really is still stale at that moment.
    deadline = time.time() + 4
    while time.time() < deadline:
        served = call(stack, consumer, "file", vault=VAULT, path=LIVE)
        if served["status"] == 200 and marker in served["body"]:
            break
        time.sleep(0.25)
    else:
        raise AssertionError("service never returned the in-flight edit")
    on_disk = stack.owner_session.get(f"{stack.url}/api/docs/{VAULT}/file", params={"path": LIVE}).text
    assert marker not in on_disk

    # And the edit does eventually reach the file, so the two views converge.
    deadline = time.time() + 30
    while time.time() < deadline:
        if marker in stack.owner_session.get(f"{stack.url}/api/docs/{VAULT}/file", params={"path": LIVE}).text:
            break
        time.sleep(1)
    else:
        raise AssertionError("the edit never reached the .md file")
