import json
from pathlib import Path

from server.core.config import Config
from server.core.service_grants import NotesGrant
from server.core.service_grants import allows
from server.core.service_grants import build_grant
from server.core.service_grants import grant_page_url
from server.core.service_grants import granted_vaults
from server.core.service_grants import parse_access
from server.core.service_grants import parse_permissions_header
from server.core.service_grants import validated_return_to

CONFIG = Config(
    vault_path=Path("/tmp/vault"),
    db_path=Path("/tmp/main.db"),
    app_origin="https://md-notes.alice.example.com",
    zone_domain="alice.example.com",
)


def header(*entries: object) -> str:
    return json.dumps(list(entries))


def app_scoped(vault: str, paths: object = "*", access: str = "read") -> dict[str, object]:
    return {"grant": {"vault": vault, "paths": paths, "access": access}, "scope": "app"}


def test_whole_vault_grant_covers_any_path() -> None:
    grants = parse_permissions_header(header(app_scoped("personal")))
    assert allows(grants, "personal", "inbox.md", "read")
    assert allows(grants, "personal", "deep/nested/note.md", "read")
    assert not allows(grants, "work", "inbox.md", "read")


def test_file_list_grant_covers_only_those_files() -> None:
    grants = parse_permissions_header(header(app_scoped("personal", ["a.md", "sub/b.md"])))
    assert allows(grants, "personal", "a.md", "read")
    assert allows(grants, "personal", "sub/b.md", "read")
    assert not allows(grants, "personal", "sub/c.md", "read")


def test_higher_tiers_imply_read() -> None:
    grants = parse_permissions_header(header(app_scoped("personal", access="write")))
    assert allows(grants, "personal", "a.md", "read")
    assert allows(grants, "personal", "a.md", "comment")
    assert allows(grants, "personal", "a.md", "write")


def test_read_grant_does_not_imply_write() -> None:
    grants = parse_permissions_header(header(app_scoped("personal")))
    assert not allows(grants, "personal", "a.md", "write")


def test_global_scoped_grants_are_ignored() -> None:
    entry = app_scoped("personal")
    entry["scope"] = "global"
    assert parse_permissions_header(header(entry)) == ()


def test_malformed_entries_are_dropped_not_fatal() -> None:
    good = app_scoped("personal")
    assert parse_permissions_header(header("read", {"grant": "read", "scope": "app"}, 7, good)) == (
        NotesGrant(vault="personal", paths=None, access="read"),
    )
    assert parse_permissions_header(header(app_scoped("personal", access="admin"))) == ()
    assert parse_permissions_header(header(app_scoped("", "*"))) == ()
    assert parse_permissions_header(header(app_scoped("personal", paths=5))) == ()
    assert parse_permissions_header("not json") == ()
    assert parse_permissions_header(None) == ()


def test_granted_vaults_deduplicates_and_respects_tier() -> None:
    grants = parse_permissions_header(
        header(app_scoped("personal", ["a.md"]), app_scoped("personal", ["b.md"]), app_scoped("work"))
    )
    assert granted_vaults(grants, "read") == ["personal", "work"]
    assert granted_vaults(grants, "write") == []


def test_build_grant_normalises_paths() -> None:
    grant = build_grant("personal", ["/b.md", "a.md", "a.md", "  ", " c.md "], "read")
    assert grant.paths == ("a.md", "b.md", "c.md")
    assert grant.to_payload() == {"vault": "personal", "paths": ["a.md", "b.md", "c.md"], "access": "read"}


def test_build_whole_vault_grant() -> None:
    assert build_grant("personal", None, "read").to_payload() == {
        "vault": "personal",
        "paths": "*",
        "access": "read",
    }


def test_grant_payload_round_trips_through_the_header() -> None:
    grant = build_grant("personal", ["a.md"], "read")
    assert parse_permissions_header(header({"grant": grant.to_payload(), "scope": "app"})) == (grant,)


def test_grant_page_url_names_the_requester_and_tier() -> None:
    assert grant_page_url(CONFIG, "photos-app", "read") == (
        "https://md-notes.alice.example.com/service/grant?consumer=photos-app&access=read"
    )


def test_grant_page_url_escapes_the_name() -> None:
    assert "consumer=odd+%26+name" in grant_page_url(CONFIG, "odd & name", "read")


def test_parse_access_accepts_only_real_tiers() -> None:
    assert [parse_access(t) for t in ("read", "comment", "write")] == ["read", "comment", "write"]
    assert parse_access("admin") is None
    assert parse_access("") is None
    assert parse_access(None) is None


def test_return_to_must_stay_inside_the_zone() -> None:
    assert validated_return_to(CONFIG, "https://photos.alice.example.com/x") == "https://photos.alice.example.com/x"
    assert validated_return_to(CONFIG, "https://alice.example.com/") == "https://alice.example.com/"
    assert validated_return_to(CONFIG, "https://evil.com/") is None
    assert validated_return_to(CONFIG, "https://alice.example.com.evil.com/") is None
    assert validated_return_to(CONFIG, "javascript:alert(1)") is None
    assert validated_return_to(CONFIG, None) is None
