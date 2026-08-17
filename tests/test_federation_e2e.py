"""Two-instance federation tests: instance A shares a vault, instance B connects to it.

Runs two full OpenHost stacks (two routers, two md-notes containers) side by side. Everything
crosses instances through the browser (requests + Playwright, both running on the host, like a
real user's browser): B's server only stores the connection record and never contacts A.
"""

import time

import pytest
import requests
from openhost_test_harness import OpenhostStack
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.containers

VAULT = "fedvault"
FILE = "shared.md"
FILE_CONTENT = "# Shared\n\nGreetings from instance A."


def _wait_healthy(stack: OpenhostStack, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = stack.owner_session.get(f"{stack.url}/health", timeout=2)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"App did not become healthy at {stack.url}/health within {timeout}s")


# Instance A is the shared session-scoped `stack` fixture from conftest (app name "md-notes").
# Instance B needs a distinct app name because podman container names are global.
@pytest.fixture(scope="module")
def stack_b():
    with OpenhostStack(app_name="md-notes-b", zone_name="zoneb") as s:
        _wait_healthy(s)
        yield s


@pytest.fixture(scope="module", autouse=True)
def _seed_vault(stack: OpenhostStack) -> None:
    s = stack.owner_session
    s.post(f"{stack.url}/api/vaults", json={"name": VAULT})
    r = s.post(f"{stack.url}/api/docs/{VAULT}/file?path={FILE}", json={"content": FILE_CONTENT})
    # 409 = already seeded: pytest re-runs this fixture when browser parametrization splits the
    # module's tests, and the stack it seeds into outlives the module.
    assert r.status_code in (201, 409), r.text


def _create_share(stack: OpenhostStack, name: str, permission: str) -> dict:
    r = stack.owner_session.post(
        f"{stack.url}/api/federation/shares",
        json={"vaultName": VAULT, "name": name, "permission": permission},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_invite_url_shape(stack: OpenhostStack) -> None:
    share = _create_share(stack, "invite-shape", "read")
    # The invite is a link to the sharing instance itself.
    assert share["invite_url"].startswith(f"{stack.url}/federation/connect?")
    assert f"secret={share['secret']}" in share["invite_url"]
    assert f"vault={VAULT}" in share["invite_url"]
    stack.owner_session.delete(f"{stack.url}/api/federation/shares/{share['secret']}")


def test_share_vault_via_ui(stack: OpenhostStack, page: Page) -> None:
    """The share dialog opens from the vault menu (regression: the menu's focus-restore used to
    dismiss it instantly) and creates a named share with an invite link."""
    stack.playwright_login(page)
    page.goto(f"{stack.url}/{VAULT}")
    page.wait_for_selector("#sidebar", timeout=10_000)
    page.locator(".sidebar-vault-trigger").click()
    page.get_by_text("Share this vault", exact=False).first.click()
    expect(page.locator(".share-modal")).to_be_visible()

    page.fill(".share-modal .settings-input", "ui-recipient")
    page.get_by_role("button", name="Can comment", exact=True).click()
    expect(page.locator(".share-link-row")).to_have_count(1)
    expect(page.locator(".share-link-badge")).to_contain_text("Can comment")
    invite_url = page.locator(".share-modal-link").input_value()
    assert "/federation/connect?" in invite_url and "secret=" in invite_url

    shares = stack.owner_session.get(f"{stack.url}/api/federation/shares").json()
    created = [s for s in shares if s["share_name"] == "ui-recipient"]
    assert len(created) == 1 and created[0]["permission"] == "comment"
    stack.owner_session.delete(f"{stack.url}/api/federation/shares/{created[0]['secret']}")


def test_cross_origin_vault_api(stack: OpenhostStack) -> None:
    """Simulates instance B's browser hitting instance A's unified vault API with a secret."""
    write_share = _create_share(stack, "bob", "write")
    read_share = _create_share(stack, "carol", "read")
    base = f"{stack.url}/api/docs/{VAULT}"
    # Own file: pytest groups the browser-parametrized tests together, so tests that edit FILE
    # can run before this one regardless of definition order.
    r = stack.owner_session.post(f"{base}/file", params={"path": "cross-origin.md"}, json={"content": "untouched"})
    assert r.status_code == 201, r.text
    anon = requests.Session()  # no owner cookies — a foreign browser

    # share-info handshake
    r = anon.get(f"{stack.url}/api/federation/share-info", params={"secret": write_share["secret"]})
    assert r.status_code == 200
    assert r.json() == {"app": "md-notes", "api_version": 1, "vault": VAULT, "permission": "write"}
    assert anon.get(f"{stack.url}/api/federation/share-info", params={"secret": "nope"}).status_code == 401

    # listing + reading
    assert anon.get(base).status_code == 401
    r = anon.get(base, params={"secret": read_share["secret"]})
    assert r.status_code == 200
    assert "cross-origin.md" in [e["path"] for e in r.json()]
    r = anon.get(f"{base}/file", params={"secret": read_share["secret"], "path": "cross-origin.md"})
    assert r.text == "untouched"

    # writes: allowed for write shares, rejected for read shares
    r = anon.post(
        f"{base}/file",
        params={"secret": write_share["secret"], "path": "from-b.md"},
        json={"content": "written by B", "type": "file"},
    )
    assert r.status_code == 201, r.text
    r = anon.post(
        f"{base}/file",
        params={"secret": read_share["secret"], "path": "nope.md"},
        json={"content": "x", "type": "file"},
    )
    assert r.status_code == 401

    # revocation cuts access immediately
    assert stack.owner_session.delete(f"{stack.url}/api/federation/shares/{read_share['secret']}").status_code == 200
    assert anon.get(base, params={"secret": read_share["secret"]}).status_code == 401

    stack.owner_session.delete(f"{stack.url}/api/federation/shares/{write_share['secret']}")
    anon.delete(f"{base}/file", params={"secret": write_share["secret"], "path": "from-b.md"})


def test_connect_and_unified_vault_list(stack: OpenhostStack, stack_b: OpenhostStack) -> None:
    """Instance B stores a connection record and lists it alongside its own vaults."""
    share = _create_share(stack, "instance-b", "comment")
    sb = stack_b.owner_session

    r = sb.post(
        f"{stack_b.url}/api/vaults/connections",
        json={"host": stack.url, "vault": VAULT, "secret": share["secret"], "permission": "comment", "name": ""},
    )
    assert r.status_code == 201, r.text
    connected = r.json()
    assert connected["owned"] is False
    assert connected["host"] == stack.url
    assert connected["vault"] == VAULT
    assert connected["permission"] == "comment"

    # Idempotent on host+secret.
    again = sb.post(
        f"{stack_b.url}/api/vaults/connections",
        json={"host": stack.url, "vault": VAULT, "secret": share["secret"], "permission": "comment", "name": ""},
    )
    assert again.json()["id"] == connected["id"]

    vaults = sb.get(f"{stack_b.url}/api/vaults").json()
    assert [v["owned"] for v in vaults if v["id"] == connected["id"]] == [False]

    assert sb.delete(f"{stack_b.url}/api/vaults/connections/{connected['id']}").status_code == 200
    stack.owner_session.delete(f"{stack.url}/api/federation/shares/{share['secret']}")


def test_invite_landing_page(stack: OpenhostStack, page: Page) -> None:
    """Opening the invite link directly shows an informational page on the sharing instance."""
    share = _create_share(stack, "landing", "read")
    page.goto(share["invite_url"])
    expect(page.locator(".vault-picker-title")).to_contain_text("Shared vault invite")
    expect(page.locator(".vault-picker-card")).to_contain_text(VAULT)
    expect(page.locator(".vault-picker-card")).to_contain_text("view only")
    # Recipients without their own instance can discover the app.
    github = page.locator('.federation-connect-footer a[href="https://github.com/cloud-in-a-bottle/md-notes"]')
    expect(github).to_be_visible()
    stack.owner_session.delete(f"{stack.url}/api/federation/shares/{share['secret']}")


def _connect_via_ui(stack: OpenhostStack, stack_b: OpenhostStack, page: Page, permission: str) -> dict:
    """The real user flow: B pastes A's invite link into their own vault picker."""
    share = _create_share(stack, f"ui-{permission}", permission)
    stack_b.playwright_login(page)
    page.goto(stack_b.url)
    page.fill('input[placeholder^="Paste an invite link"]', share["invite_url"])
    page.get_by_role("button", name="Connect", exact=True).click()
    page.locator(f'.sidebar-item[data-type="file"][data-path="{FILE}"]').click()
    page.wait_for_selector(".cm-editor", timeout=15_000)
    page.wait_for_timeout(1500)
    return share


def test_b_connects_by_pasting_invite_and_edits(stack: OpenhostStack, stack_b: OpenhostStack, page: Page) -> None:
    share = _connect_via_ui(stack, stack_b, page, "write")
    content = page.locator(".cm-content")
    expect(content).to_contain_text("Greetings from instance A.")

    content.click()
    page.keyboard.press("Escape")
    page.keyboard.type("G")
    page.keyboard.type("o")
    page.keyboard.type("EDITED_FROM_B")
    page.keyboard.press("Escape")
    expect(content).to_contain_text("EDITED_FROM_B")

    # The CRDT room on A debounce-saves within ~10s; the edit must land in A's own .md file.
    deadline = time.time() + 30
    while time.time() < deadline:
        body = stack.owner_session.get(f"{stack.url}/api/docs/{VAULT}/file", params={"path": FILE}).text
        if "EDITED_FROM_B" in body:
            break
        time.sleep(2)
    else:
        raise AssertionError("Edit from instance B never reached instance A's markdown file")

    # A's owner opens the same doc and sees B's cursor labeled with B's OpenHost username,
    # not "Anonymous".
    b_username = stack_b.owner_session.get(f"{stack_b.url}/api/settings/me").json()["displayName"]
    content.click()  # keep B's cursor live in the doc
    browser = page.context.browser
    assert browser is not None
    ctx_a = browser.new_context()
    page_a = ctx_a.new_page()
    stack.playwright_login(page_a)
    page_a.goto(f"{stack.url}/{VAULT}")
    page_a.locator(f'.sidebar-item[data-type="file"][data-path="{FILE}"]').click()
    page_a.wait_for_selector(".cm-editor", timeout=15_000)
    expect(page_a.locator(".cm-ySelectionInfo").first).to_have_text(b_username, timeout=10_000)
    assert "Anonymous" not in page_a.locator(".cm-editor").inner_text()
    ctx_a.close()

    _cleanup_connection(stack, stack_b, share)


def test_readonly_connected_vault_ui(stack: OpenhostStack, stack_b: OpenhostStack, page: Page) -> None:
    share = _connect_via_ui(stack, stack_b, page, "read")
    content = page.locator(".cm-content")
    expect(content).to_contain_text("Greetings from instance A.")
    # CodeMirror readOnly + editable(false) renders a non-editable content element.
    assert content.get_attribute("contenteditable") == "false"
    content.click()
    page.keyboard.type("SHOULD_NOT_APPEAR")
    page.wait_for_timeout(500)
    # Typing must not change the doc locally (live-preview may toggle formatting marks with the
    # cursor, so check content rather than exact rendering) nor ever reach instance A.
    assert "SHOULD_NOT_APPEAR" not in content.inner_text()
    body = stack.owner_session.get(f"{stack.url}/api/docs/{VAULT}/file", params={"path": FILE}).text
    assert "SHOULD_NOT_APPEAR" not in body

    _cleanup_connection(stack, stack_b, share)


def test_comment_tier_connected_vault_ui(stack: OpenhostStack, stack_b: OpenhostStack, page: Page) -> None:
    """Comment tier: doc is not editable in the UI, but the comment API accepts the secret."""
    share = _connect_via_ui(stack, stack_b, page, "comment")
    content = page.locator(".cm-content")
    expect(content).to_contain_text("Greetings from instance A.")
    assert content.get_attribute("contenteditable") == "false"
    # The comments panel offers "add comment" affordances only when canComment; at minimum the
    # comment-tier secret must pass A's auth for the comments route (400 = domain validation,
    # not 401 = rejected).
    anon = requests.Session()
    r = anon.post(
        f"{stack.url}/api/docs/{VAULT}/comments",
        params={"path": FILE, "secret": share["secret"]},
        json={"userId": "u1", "userName": "B", "text": "hi", "anchorStart": None, "anchorEnd": None},
    )
    assert r.status_code == 400, r.text

    _cleanup_connection(stack, stack_b, share)


def _cleanup_connection(stack: OpenhostStack, stack_b: OpenhostStack, share: dict) -> None:
    sb = stack_b.owner_session
    for vault in sb.get(f"{stack_b.url}/api/vaults").json():
        if not vault["owned"]:
            sb.delete(f"{stack_b.url}/api/vaults/connections/{vault['id']}")
    stack.owner_session.delete(f"{stack.url}/api/federation/shares/{share['secret']}")
