"""Deleted/renamed docs must stay deleted: sidecars go with them, and live rooms can't write them back."""

import asyncio
from collections.abc import Callable
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from pycrdt import Text

from server.core import crdt_store
from server.core.files import create_file
from server.core.files import delete_file
from server.core.files import rename_file
from server.core.sync import SyncManager
from server.core.vaults import delete_vault
from server.core.vaults import rename_vault


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    """Vaults root + one vault, matching the production layout (sidecars at ``<vaults>_crdt/<vault>/...``)."""
    vaults = tmp_path / "vault"
    vault = vaults / "v"
    vault.mkdir(parents=True)
    return vaults, vault


# ── sidecar tree stays in step with file ops ────────────────────────────


def test_delete_file_removes_sidecar(tmp_path: Path) -> None:
    vaults, vault = _setup(tmp_path)
    create_file(vault, "a.md", "hi")
    crdt_store.write_state(vaults, "v/a.md", b"state")
    delete_file(vault, "a.md")
    assert crdt_store.read_state(vaults, "v/a.md") is None


def test_delete_dir_removes_sidecars(tmp_path: Path) -> None:
    vaults, vault = _setup(tmp_path)
    create_file(vault, "dir/a.md", "hi")
    crdt_store.write_state(vaults, "v/dir/a.md", b"state")
    delete_file(vault, "dir")
    assert crdt_store.read_state(vaults, "v/dir/a.md") is None


def test_rename_file_moves_sidecar(tmp_path: Path) -> None:
    vaults, vault = _setup(tmp_path)
    create_file(vault, "a.md", "hi")
    crdt_store.write_state(vaults, "v/a.md", b"state")
    rename_file(vault, "a.md", "b.md")
    assert crdt_store.read_state(vaults, "v/a.md") is None
    assert crdt_store.read_state(vaults, "v/b.md") == b"state"


def test_rename_dir_moves_sidecars(tmp_path: Path) -> None:
    vaults, vault = _setup(tmp_path)
    create_file(vault, "src/a.md", "hi")
    crdt_store.write_state(vaults, "v/src/a.md", b"state")
    rename_file(vault, "src", "dst")
    assert crdt_store.read_state(vaults, "v/src/a.md") is None
    assert crdt_store.read_state(vaults, "v/dst/a.md") == b"state"


def test_delete_vault_removes_sidecars(tmp_path: Path) -> None:
    vaults, vault = _setup(tmp_path)
    create_file(vault, "a.md", "hi")
    crdt_store.write_state(vaults, "v/a.md", b"state")
    delete_vault(vaults, "v")
    assert crdt_store.read_state(vaults, "v/a.md") is None


def test_rename_vault_moves_sidecars(tmp_path: Path) -> None:
    vaults, vault = _setup(tmp_path)
    create_file(vault, "a.md", "hi")
    crdt_store.write_state(vaults, "v/a.md", b"state")
    rename_vault(vaults, "v", "w")
    assert crdt_store.read_state(vaults, "v/a.md") is None
    assert crdt_store.read_state(vaults, "w/a.md") == b"state"


# ── live rooms can't resurrect deleted docs ─────────────────────────────


async def _open_room(manager: SyncManager, doc_path: str) -> Any:
    assert manager._ws_server is not None
    room = await manager._ws_server.get_room(doc_path)
    manager._init_room(room, doc_path)
    return room


def test_save_room_skips_deleted_md(tmp_path: Path, run_async: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    async def run() -> None:
        vaults, vault = _setup(tmp_path)
        create_file(vault, "a.md", "hello")
        manager = SyncManager(vaults)
        await manager.start()
        room = await _open_room(manager, "v/a.md")
        text = room.ydoc.get("content", type=Text)
        text += " edited"

        delete_file(vault, "a.md")
        await manager._save_room("v/a.md", room)
        assert not (vault / "a.md").exists()
        assert crdt_store.read_state(vaults, "v/a.md") is None

        # Shutdown save-all must not resurrect it either.
        await manager.stop()
        assert not (vault / "a.md").exists()

    run_async(run())


def test_close_rooms_drops_room_and_pending_save(
    tmp_path: Path, run_async: Callable[[Coroutine[Any, Any, Any]], Any]
) -> None:
    async def run() -> None:
        vaults, vault = _setup(tmp_path)
        create_file(vault, "a.md", "hello")
        manager = SyncManager(vaults)
        await manager.start()
        room = await _open_room(manager, "v/a.md")
        text = room.ydoc.get("content", type=Text)
        text += " edited"
        assert "v/a.md" in manager._save_tasks

        await manager.close_rooms("v/a.md", save=False, reason="Document deleted")
        delete_file(vault, "a.md")

        assert manager._ws_server is not None
        assert "v/a.md" not in manager._ws_server.rooms
        assert "v/a.md" not in manager._initialised_rooms
        await asyncio.sleep(0.05)  # let the cancelled save task settle
        assert not (vault / "a.md").exists()

        # The websocket server must survive closing a freshly-created room (pycrdt's room startup
        # races teardown; a naive delete_room here kills the server's task group).
        assert manager._ws_server_task is not None and not manager._ws_server_task.done()

        # A fresh room for the deleted path must be refused, not recreated.
        room2 = await manager._ws_server.get_room("v/a.md")
        try:
            manager._init_room(room2, "v/a.md")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("expected FileNotFoundError for deleted .md")
        await manager.stop()
        assert not (vault / "a.md").exists()

    run_async(run())


def test_close_rooms_with_save_flushes_content(
    tmp_path: Path, run_async: Callable[[Coroutine[Any, Any, Any]], Any]
) -> None:
    async def run() -> None:
        vaults, vault = _setup(tmp_path)
        create_file(vault, "a.md", "hello")
        manager = SyncManager(vaults)
        await manager.start()
        room = await _open_room(manager, "v/a.md")
        text = room.ydoc.get("content", type=Text)
        text += " edited"

        await manager.close_rooms("v/a.md", save=True, reason="Document moved")
        assert (vault / "a.md").read_text() == "hello edited"
        assert crdt_store.read_state(vaults, "v/a.md") is not None
        await manager.stop()

    run_async(run())


def test_close_rooms_prefix_covers_directories(
    tmp_path: Path, run_async: Callable[[Coroutine[Any, Any, Any]], Any]
) -> None:
    async def run() -> None:
        vaults, vault = _setup(tmp_path)
        create_file(vault, "dir/a.md", "one")
        create_file(vault, "dir/b.md", "two")
        create_file(vault, "other.md", "three")
        manager = SyncManager(vaults)
        await manager.start()
        for path in ("v/dir/a.md", "v/dir/b.md", "v/other.md"):
            await _open_room(manager, path)

        await manager.close_rooms("v/dir", save=False, reason="Document deleted")
        assert manager._ws_server is not None
        assert "v/dir/a.md" not in manager._ws_server.rooms
        assert "v/dir/b.md" not in manager._ws_server.rooms
        assert "v/other.md" in manager._ws_server.rooms
        await manager.stop()

    run_async(run())


class FakeChannel:
    """Minimal pycrdt Channel: blocks on a queue until closed."""

    def __init__(self, path: str):
        self.path = path
        self.closed: tuple[int, str] | None = None
        self.sent: list[bytes] = []
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def __aiter__(self) -> "FakeChannel":
        return self

    async def __anext__(self) -> bytes:
        data = await self._queue.get()
        if data is None:
            raise StopAsyncIteration
        return data

    async def send(self, message: bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> bytes:
        data = await self._queue.get()
        assert data is not None
        return data

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)
        await self._queue.put(None)


def test_close_rooms_disconnects_clients_without_zombie_cleanup(
    tmp_path: Path, run_async: Callable[[Coroutine[Any, Any, Any]], Any]
) -> None:
    async def run() -> None:
        vaults, vault = _setup(tmp_path)
        create_file(vault, "a.md", "hello")
        manager = SyncManager(vaults)
        await manager.start()

        channel = FakeChannel("v/a.md")
        serve_task = asyncio.create_task(manager.serve(channel))
        assert manager._ws_server is not None
        for _ in range(100):
            await asyncio.sleep(0.01)
            room = manager._ws_server.rooms.get("v/a.md")
            if room is not None and room.clients:
                break
        else:
            raise AssertionError("client never joined the room")

        await manager.close_rooms("v/a.md", save=False, reason="Document deleted")
        await asyncio.wait_for(serve_task, timeout=2)
        assert channel.closed == (4404, "Document deleted")
        # In-band doc-gone frame (type 100 + varstring reason) — close codes don't survive the router.
        assert channel.sent[-1] == bytes([100, len(b"Document deleted")]) + b"Document deleted"
        assert "v/a.md" not in manager._ws_server.rooms
        # The disconnect must not have scheduled a grace-period cleanup (whose save would resurrect the doc).
        assert "v/a.md" not in manager._cleanup_tasks

        delete_file(vault, "a.md")
        await asyncio.sleep(0.05)
        assert not (vault / "a.md").exists()
        await manager.stop()
        assert not (vault / "a.md").exists()

    run_async(run())
