"""Yjs document sync: room lifecycle and channel filtering.

Each document is identified by its path within the vault (e.g. ``myvault/notes/foo.md``); the same string keys
both the on-disk file and the in-memory Yjs room. Markdown files on disk are the source of truth; the CRDT is
an ephemeral coordination layer that exists only while clients are connected.

A room's ``Y.Doc`` holds a single ``Y.Text`` named ``content``. Round-tripping is a pure string identity:
``read_file(...)`` seeds the text on first connect, ``str(text)`` writes it back on save.

The full lifecycle for each room — disk read on first connect, debounced saves while editing, save-and-drop on
last disconnect, save-all on shutdown — is owned by ``SyncManager``. One instance per app, constructed in
``create_app`` and stored on ``app.state.sync_manager``; route handlers look it up off the connection and call
``serve(channel)`` with a Channel-protocol object (any object with ``path``, ``__aiter__``, ``send``, ``recv``).

REST file writes bypass this layer entirely — they edit the ``.md`` directly. A REST write to a doc that has a
live room will be overwritten on the room's next save. Delete/rename endpoints therefore call ``close_rooms``
first, which drops any live room (disconnecting its clients) so it can't write the file back; as a backstop,
``_save_room`` refuses to save a doc whose ``.md`` no longer exists.
"""

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import ClassVar
from typing import Self
from typing import TypeVar

from loguru import logger
from pycrdt import Doc
from pycrdt import Subscription
from pycrdt import Text
from pycrdt.websocket import WebsocketServer
from pycrdt.websocket import YRoom

from server.core import crdt_store
from server.core.comments import gc_orphaned_comments
from server.core.files import PathTraversalError
from server.core.files import file_exists
from server.core.files import read_file
from server.core.files import write_file

T = TypeVar("T")


class SyncNotRunning(Exception):
    """Raised by ``SyncManager.serve`` when the underlying WebSocket server is not started."""


# ── Channel wrappers ────────────────────────────────────────────────────


_MSG_SYNC = 0
_SYNC_UPDATE = 2

# Yjs sync updates above this size are logged at INFO. Most edits are <100 bytes; an update in
# the kilobytes is suspicious — typically a stale client reconciling its full local state with
# the server's, which is the pattern that produces content duplication.
_LARGE_UPDATE_BYTES = 2048

# WebSocket close code sent to clients when their doc's room is force-closed because the doc (or
# its vault) was deleted or moved. Clients must stop reconnecting and drop local caches —
# retrying would merge their orphaned state into whatever doc later reuses the path.
DOC_GONE_CLOSE_CODE = 4404

# The OpenHost router does not preserve WebSocket close codes (clients observe 1011), so the same
# signal also rides the Yjs channel as a data frame right before the close: varuint message type
# (unused by y-protocols; y-websocket uses 0-3) + varstring reason, decodable by lib0.
_MSG_DOC_GONE = 100


def _doc_gone_message(reason: str) -> bytes:
    payload = reason.encode()
    assert len(payload) < 128  # single-byte varuint length
    return bytes([_MSG_DOC_GONE, len(payload)]) + payload


class ReadOnlyChannel:
    """Wraps a channel to enforce read-only access.

    Allows the initial sync handshake (sync step 1 + step 2) so the client receives the document contents,
    but drops any subsequent Yjs sync updates from the client. Awareness messages pass through.
    """

    def __init__(self, inner: Any):
        self._inner = inner

    @property
    def path(self) -> str:
        return self._inner.path  # type: ignore[no-any-return]

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        while True:
            data = await self._inner.__anext__()
            if len(data) < 2:
                return data  # type: ignore[no-any-return]
            if data[0] == _MSG_SYNC and data[1] == _SYNC_UPDATE:
                logger.debug("Dropped update from read-only client")
                continue
            return data  # type: ignore[no-any-return]

    async def send(self, message: bytes) -> None:
        await self._inner.send(message)

    async def recv(self) -> bytes:
        return await self._inner.recv()  # type: ignore[no-any-return]

    async def close(self, code: int, reason: str) -> None:
        await self._inner.close(code, reason)


class LoggingChannel:
    """Wraps a channel to log suspiciously-large incoming Yjs sync updates at INFO."""

    def __init__(self, inner: Any, client_id: str):
        self._inner = inner
        self._client_id = client_id

    @property
    def path(self) -> str:
        return self._inner.path  # type: ignore[no-any-return]

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        data: bytes = await self._inner.__anext__()
        if len(data) >= 2 and data[0] == _MSG_SYNC and data[1] == _SYNC_UPDATE and len(data) >= _LARGE_UPDATE_BYTES:
            logger.info(
                "Large sync update from client {} on {}: {} bytes (possible stale-state merge)",
                self._client_id,
                self._inner.path,
                len(data),
            )
        return data

    async def send(self, message: bytes) -> None:
        await self._inner.send(message)

    async def recv(self) -> bytes:
        return await self._inner.recv()  # type: ignore[no-any-return]

    async def close(self, code: int, reason: str) -> None:
        await self._inner.close(code, reason)


# ── Sync manager ────────────────────────────────────────────────────────


class SyncManager:
    """Owns the pycrdt WebSocket server and the lifecycle of every active room."""

    SAVE_DEBOUNCE_SECS: ClassVar[float] = 5.0
    SAVE_MAX_INTERVAL_SECS: ClassVar[float] = 10.0
    ROOM_CLEANUP_DELAY_SECS: ClassVar[float] = 300.0

    def __init__(self, vault_path: Path) -> None:
        self._vault_path = vault_path
        self._ws_server: WebsocketServer | None = None
        self._ws_server_task: asyncio.Task[None] | None = None
        self._initialised_rooms: set[str] = set()
        self._save_tasks: dict[str, asyncio.Task[None]] = {}
        self._first_dirty_at: dict[str, float] = {}
        self._cleanup_tasks: dict[str, asyncio.Task[None]] = {}
        self._last_saved_size: dict[str, int] = {}
        self._subscriptions: dict[str, Subscription] = {}

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        self._ws_server = WebsocketServer(rooms_ready=False, auto_clean_rooms=False)
        self._ws_server_task = asyncio.ensure_future(self._ws_server.start())
        await asyncio.sleep(0.1)
        logger.info("Yjs WebSocket server started")

    async def stop(self) -> None:
        if self._ws_server is None:
            return
        for task in self._cleanup_tasks.values():
            task.cancel()
        self._cleanup_tasks.clear()
        for room_name in list(self._initialised_rooms):
            try:
                room = await self._ws_server.get_room(room_name)
                await self._save_room(room_name, room)
            except Exception:
                logger.exception("Failed to save room {} during shutdown", room_name)
        await self._ws_server.stop()
        if self._ws_server_task:
            self._ws_server_task.cancel()
            self._ws_server_task = None
        self._ws_server = None
        self._initialised_rooms.clear()
        self._first_dirty_at.clear()
        self._subscriptions.clear()
        logger.info("Yjs WebSocket server stopped")

    async def _delete_room(self, room: YRoom) -> None:
        """``delete_room``, but only after the room's background startup has settled.

        pycrdt sets ``room.started`` before the room's awareness/provider tasks actually spawn; stopping a
        room inside that window makes ``YRoom._start`` resume against a nulled task group, and the resulting
        crash takes down the WebsocketServer's whole task group — silently killing sync for every doc. The
        awareness task group is the last thing ``_start`` awaits, so once it exists (plus one scheduler tick
        for the remaining synchronous step) the room is safe to stop.
        """
        assert self._ws_server is not None
        for _ in range(500):
            if room.awareness._task_group is not None:
                break
            await asyncio.sleep(0.01)
        else:
            logger.warning("Room startup never settled; deleting anyway")
        await asyncio.sleep(0.01)
        await self._ws_server.delete_room(room=room)

    # ── public serve ────────────────────────────────────────────────────

    async def serve(self, channel: Any) -> None:
        """Serve a Yjs sync session on ``channel``.

        ``channel`` must conform to pycrdt's Channel protocol: a ``path`` property, async iteration yielding
        bytes, and ``send``/``recv`` of bytes. Raises ``SyncNotRunning`` if the server is not started.
        """
        if self._ws_server is None:
            raise SyncNotRunning()

        doc_path = channel.path
        client_id = uuid.uuid4().hex[:8]

        pending = self._cleanup_tasks.pop(doc_path, None)
        if pending and not pending.done():
            pending.cancel()
            logger.info("Cancelled pending cleanup for {} — new client joining", doc_path)

        room = await self._ws_server.get_room(doc_path)
        try:
            self._init_room(room, doc_path)
        except FileNotFoundError:
            await self._delete_room(room)
            logger.info("Refusing room {}: .md not found", doc_path)
            raise
        room.ready = True

        # Wrap original channel (which may already be ReadOnlyChannel) so we get visibility into
        # large incoming updates. The wrapper preserves .path for downstream room routing.
        if not isinstance(channel, LoggingChannel):
            channel = LoggingChannel(channel, client_id)

        logger.info(
            "Client {} joined {} (now {} client(s))",
            client_id,
            doc_path,
            len(room.clients) + 1,
        )

        try:
            await self._ws_server.serve(channel)
        finally:
            remaining = len(room.clients)
            logger.info(
                "Client {} left {} ({} client(s) remain)",
                client_id,
                doc_path,
                remaining,
            )
            # Identity check: skip cleanup if the room was force-closed (deleted/renamed) while
            # this client was being served — scheduling would save the doc right back to disk.
            if not room.clients and self._ws_server and self._ws_server.rooms.get(doc_path) is room:
                self._schedule_room_cleanup(doc_path, room)

    async def mutate_doc(self, doc_path: str, mutate: Callable[[Doc[Any]], T]) -> T:
        """Apply a synchronous mutation to a document's live Y.Doc (loading the room if needed).

        This is how REST endpoints (comments) write into the CRDT: the room broadcasts the resulting update to
        every connected client, and the doc-level observer schedules the usual debounced save. Raises
        ``SyncNotRunning`` / ``FileNotFoundError`` like ``serve``.
        """
        if self._ws_server is None:
            raise SyncNotRunning()

        room = await self._ws_server.get_room(doc_path)
        # get_room launches the room's provider in the background; stopping (delete_room) or mutating
        # before it has started races its awareness/broadcast startup.
        await room.started.wait()
        try:
            self._init_room(room, doc_path)
        except FileNotFoundError:
            await self._delete_room(room)
            raise
        room.ready = True

        result = mutate(room.ydoc)

        # A REST mutation may have created the room with no websocket clients; make sure it doesn't
        # linger in memory forever (serve() cancels this if a client joins).
        if not room.clients and doc_path not in self._cleanup_tasks:
            self._schedule_room_cleanup(doc_path, room)
        return result

    async def read_doc(self, doc_path: str) -> str:
        """The document's current text, read from its live Y.Doc — so in-flight edits are included.

        Loads the room if nobody has it open; ``mutate_doc``'s cleanup scheduling then drops it
        again after the usual grace period. Raises ``FileNotFoundError`` if the .md is gone.
        """

        def read(doc: Doc[Any]) -> str:
            text = doc.get("content", type=Text)
            return str(text) if text is not None else ""

        return await self.mutate_doc(doc_path, read)

    async def close_rooms(self, doc_path: str, *, save: bool, reason: str) -> None:
        """Force-close the live room at ``doc_path`` and any rooms below it (for directories/vaults).

        REST delete/rename callers must invoke this *before* touching disk: a live room would otherwise write
        the doc right back on its next save (edit debounce, cleanup grace period, or shutdown). ``save=True``
        flushes current content to the .md/sidecar first — wanted for renames, where the move carries the
        flushed state along; deletes pass ``save=False`` so nothing is written back.
        """
        if self._ws_server is None:
            return
        prefix = doc_path.rstrip("/") + "/"
        for name in list(self._ws_server.rooms):
            if name == doc_path or name.startswith(prefix):
                await self._close_room(name, save=save, reason=reason)

    async def _close_room(self, doc_path: str, *, save: bool, reason: str) -> None:
        assert self._ws_server is not None
        room = self._ws_server.rooms.get(doc_path)
        if room is None:
            return
        for tasks in (self._save_tasks, self._cleanup_tasks):
            task = tasks.pop(doc_path, None)
            if task and not task.done():
                task.cancel()
        await room.started.wait()
        if save:
            await self._save_room(doc_path, room)
        # Detach our doc observer so a straggling update (a client message racing the socket close)
        # can't schedule a new save from the dead room.
        subscription = self._subscriptions.pop(doc_path, None)
        if subscription is not None:
            room.ydoc.unobserve(subscription)
        clients = list(room.clients)
        for client in clients:
            try:
                await client.send(_doc_gone_message(reason))
            except Exception:
                logger.debug("Failed to send doc-gone message on {}", doc_path)
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    await close(DOC_GONE_CLOSE_CODE, reason)
                except Exception:
                    logger.exception("Failed to close client socket on {}", doc_path)
        await self._delete_room(room)
        # A disconnecting client's serve() may have scheduled a cleanup while we waited to delete
        # the room (its save would recreate the doc) — cancel it.
        late_cleanup = self._cleanup_tasks.pop(doc_path, None)
        if late_cleanup and not late_cleanup.done():
            late_cleanup.cancel()
        self._initialised_rooms.discard(doc_path)
        self._last_saved_size.pop(doc_path, None)
        self._first_dirty_at.pop(doc_path, None)
        logger.info("Force-closed room {} ({} client(s)): {}", doc_path, len(clients), reason)

    # ── room cleanup ─────────────────────────────────────────────────────

    def _schedule_room_cleanup(self, doc_path: str, room: YRoom) -> None:
        """Schedule room deletion after a grace period instead of deleting immediately.

        Without this delay, a brief WebSocket disconnect (common on mobile, tab-switch, or network blip) causes
        the server to destroy the room and discard the Y.Doc. When the client reconnects, the server creates a
        fresh Y.Doc from disk with new internal client IDs. The reconnecting client's still-alive Y.Doc then
        syncs with this fresh doc — Yjs treats the two sets of operations as independent insertions at the same
        position, doubling every piece of content.

        The grace period keeps the room (and its Y.Doc) alive long enough for the client to reconnect to the
        *same* Y.Doc, avoiding the duplication entirely. ``serve()`` cancels a pending cleanup if a new client
        joins during the window.
        """
        existing = self._cleanup_tasks.get(doc_path)
        if existing and not existing.done():
            existing.cancel()

        logger.info(
            "Room {} idle (0 clients); cleanup scheduled in {}s",
            doc_path,
            self.ROOM_CLEANUP_DELAY_SECS,
        )

        async def _delayed_cleanup() -> None:
            await asyncio.sleep(self.ROOM_CLEANUP_DELAY_SECS)
            # Identity check: the room may have been force-closed (and possibly replaced) meanwhile.
            if self._ws_server and not room.clients and self._ws_server.rooms.get(doc_path) is room:
                await self._save_room(doc_path, room)
                await self._delete_room(room)
                self._subscriptions.pop(doc_path, None)
                self._initialised_rooms.discard(doc_path)
                self._last_saved_size.pop(doc_path, None)
                logger.info("Closed room {} after grace period", doc_path)
            self._cleanup_tasks.pop(doc_path, None)

        self._cleanup_tasks[doc_path] = asyncio.ensure_future(_delayed_cleanup())

    # ── room init / save ────────────────────────────────────────────────

    def _init_room(self, room: YRoom, room_name: str) -> None:
        """Load CRDT sidecar (or seed from .md) into the Y.Doc on first connect.

        Raises FileNotFoundError if the .md does not exist — .md is the source of truth, no file means no
        room. The sidecar at ``vault_crdt/<room_name>.bin`` (when present) preserves Y.Doc clientIDs/clocks
        across restarts so reconnecting clients sync incrementally instead of merging full state and doubling
        content.
        """
        if room_name in self._initialised_rooms:
            return

        # .md must exist. We don't auto-create — REST handles file creation.
        try:
            content = read_file(self._vault_path, room_name)
        except PathTraversalError:
            raise FileNotFoundError(room_name) from None

        doc = room.ydoc
        sidecar = crdt_store.read_state(self._vault_path, room_name)
        if sidecar is not None:
            doc.apply_update(sidecar)
            text = doc.get("content", type=Text)
            initial_chars = len(str(text)) if text is not None else 0
            source = "sidecar"
        else:
            doc["content"] = text = Text()
            if content:
                text += content
            initial_chars = len(content)
            source = ".md"

        def on_change(event: Any) -> None:
            self._schedule_save(room_name, room)

        self._subscriptions[room_name] = doc.observe(on_change)

        self._initialised_rooms.add(room_name)
        self._last_saved_size[room_name] = initial_chars
        logger.info("Opened room {} from {} ({} chars)", room_name, source, initial_chars)

    async def _save_room(self, room_name: str, room: YRoom) -> None:
        """Write Y.Doc text content back to .md, then snapshot Y.Doc state to the CRDT sidecar."""
        try:
            doc = room.ydoc
            text = doc.get("content", type=Text)
            if text is None:
                return
            # The .md is the source of truth for existence: if it was deleted, never write it back.
            if not file_exists(self._vault_path, room_name):
                self._first_dirty_at.pop(room_name, None)
                logger.info("Room {} not saved: .md no longer exists", room_name)
                return
            gc_orphaned_comments(doc, room_name)
            content = str(text)
            new_size = len(content)
            prev_size = self._last_saved_size.get(room_name)
            write_file(self._vault_path, room_name, content)
            crdt_store.write_state(self._vault_path, room_name, doc.get_update())
            self._last_saved_size[room_name] = new_size
            self._first_dirty_at.pop(room_name, None)
            if prev_size is not None and new_size > prev_size * 1.25 and new_size - prev_size > 500:
                logger.info(
                    "Room {} grew from {} to {} chars (+{}, {:.0%}) — possible duplication",
                    room_name,
                    prev_size,
                    new_size,
                    new_size - prev_size,
                    (new_size - prev_size) / max(prev_size, 1),
                )
            else:
                logger.debug("Saved room {} to disk ({} chars)", room_name, new_size)
        except Exception:
            logger.exception("Failed to save room {}", room_name)

    def _schedule_save(self, room_name: str, room: YRoom) -> None:
        # Debounce by SAVE_DEBOUNCE_SECS of quiet, but cap at SAVE_MAX_INTERVAL_SECS since the room first went
        # dirty so a continuously edited doc still gets persisted on a regular cadence.
        now = asyncio.get_event_loop().time()
        first_dirty = self._first_dirty_at.setdefault(room_name, now)

        existing = self._save_tasks.get(room_name)
        if existing and not existing.done():
            existing.cancel()

        elapsed = now - first_dirty
        delay = min(self.SAVE_DEBOUNCE_SECS, max(0.0, self.SAVE_MAX_INTERVAL_SECS - elapsed))

        async def _delayed_save() -> None:
            await asyncio.sleep(delay)
            await self._save_room(room_name, room)

        self._save_tasks[room_name] = asyncio.ensure_future(_delayed_save())
