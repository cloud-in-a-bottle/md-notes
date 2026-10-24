"""Comment CRUD, permission rules, orphan GC, and the REST → Y.Doc write path."""

import base64
from collections.abc import Callable
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from pycrdt import Assoc
from pycrdt import Doc
from pycrdt import StickyIndex
from pycrdt import Text

from server.core import crdt_store
from server.core.comments import CommentNotFound
from server.core.comments import CommentPermissionError
from server.core.comments import InvalidComment
from server.core.comments import comments_map
from server.core.comments import create_comment
from server.core.comments import delete_comment
from server.core.comments import gc_orphaned_comments
from server.core.comments import update_comment
from server.core.sync import SyncManager


def _doc(content: str = "hello brave new world") -> tuple[Doc, Text]:
    doc = Doc()
    doc["content"] = text = Text()
    text += content
    return doc, text


def _anchors(text: Text, start: int, end: int) -> tuple[str, str]:
    # Same encoding as the client's Y.encodeRelativePosition: right-assoc start, left-assoc end,
    # so the anchors hug the commented span rather than adjacent insertions.
    enc_start = base64.b64encode(text.sticky_index(start, Assoc.AFTER).encode()).decode()
    enc_end = base64.b64encode(text.sticky_index(end, Assoc.BEFORE).encode()).decode()
    return enc_start, enc_end


def _create(doc: Doc, text: Text, start: int = 6, end: int = 15, user_id: str = "u1", name: str = "Alice") -> str:
    anchor_start, anchor_end = _anchors(text, start, end)
    return create_comment(
        doc,
        user_id=user_id,
        user_name=name,
        text="a comment",
        anchor_start=anchor_start,
        anchor_end=anchor_end,
        parent_id=None,
    )


def test_create_and_read_back() -> None:
    doc, text = _doc()
    cid = _create(doc, text)
    record = comments_map(doc).get(cid)
    assert record["userId"] == "u1"
    assert record["userName"] == "Alice"
    assert record["text"] == "a comment"
    assert record["resolved"] is False
    assert record["anchorStart"] and record["anchorEnd"]


def test_create_requires_anchors_and_nonempty_span() -> None:
    doc, text = _doc()
    with pytest.raises(InvalidComment):
        create_comment(doc, user_id="u", user_name="n", text="t", anchor_start=None, anchor_end=None, parent_id=None)
    start, end = _anchors(text, 3, 3)
    with pytest.raises(InvalidComment):
        create_comment(doc, user_id="u", user_name="n", text="t", anchor_start=start, anchor_end=end, parent_id=None)
    with pytest.raises(InvalidComment):
        create_comment(
            doc, user_id="u", user_name="n", text="t", anchor_start="!garbage!", anchor_end=end, parent_id=None
        )


def test_reply_lifecycle() -> None:
    doc, text = _doc()
    cid = _create(doc, text)
    rid = create_comment(
        doc, user_id="u2", user_name="Bob", text="a reply", anchor_start=None, anchor_end=None, parent_id=cid
    )
    assert comments_map(doc).get(rid)["parentId"] == cid

    # Replying to a reply or to a missing comment fails.
    with pytest.raises(InvalidComment):
        create_comment(doc, user_id="u", user_name="n", text="t", anchor_start=None, anchor_end=None, parent_id=rid)
    with pytest.raises(CommentNotFound):
        create_comment(
            doc, user_id="u", user_name="n", text="t", anchor_start=None, anchor_end=None, parent_id="missing"
        )

    # Deleting the top-level comment cascades to replies.
    delete_comment(doc, cid, user_id="u1", is_owner=False)
    assert comments_map(doc).get(rid) is None


def test_edit_is_author_only() -> None:
    doc, text = _doc()
    cid = _create(doc, text)
    with pytest.raises(CommentPermissionError):
        update_comment(doc, cid, user_id="intruder", is_owner=True, text="hijacked", resolved=None)
    update_comment(doc, cid, user_id="u1", is_owner=False, text="edited", resolved=None)
    record = comments_map(doc).get(cid)
    assert record["text"] == "edited"
    assert record["editedAt"]


def test_resolve_and_unresolve() -> None:
    doc, text = _doc()
    cid = _create(doc, text)
    update_comment(doc, cid, user_id="someone-else", is_owner=False, text=None, resolved=True)
    assert comments_map(doc).get(cid)["resolved"] is True
    update_comment(doc, cid, user_id="someone-else", is_owner=False, text=None, resolved=False)
    assert comments_map(doc).get(cid)["resolved"] is False


def test_delete_is_author_or_owner_only() -> None:
    doc, text = _doc()
    cid = _create(doc, text)
    with pytest.raises(CommentPermissionError):
        delete_comment(doc, cid, user_id="intruder", is_owner=False)
    delete_comment(doc, cid, user_id="intruder", is_owner=True)
    assert comments_map(doc).get(cid) is None
    with pytest.raises(CommentNotFound):
        delete_comment(doc, cid, user_id="u1", is_owner=True)


def test_anchors_track_edits() -> None:
    doc, text = _doc()
    cid = _create(doc, text, start=6, end=15)  # "brave new"
    record = comments_map(doc).get(cid)
    with doc.transaction():
        text.insert(0, "PREFIX ")
    start = StickyIndex.decode(base64.b64decode(record["anchorStart"]), text)
    end = StickyIndex.decode(base64.b64decode(record["anchorEnd"]), text)
    assert (start.get_index(), end.get_index()) == (13, 22)


def test_gc_deletes_collapsed_comments_and_replies() -> None:
    doc, text = _doc()
    survivor = _create(doc, text, start=0, end=5)  # "hello"
    orphan = _create(doc, text, start=6, end=15)  # "brave new"
    reply = create_comment(
        doc, user_id="u2", user_name="Bob", text="reply", anchor_start=None, anchor_end=None, parent_id=orphan
    )
    with doc.transaction():
        del text[6:16]  # delete "brave new " entirely

    assert gc_orphaned_comments(doc, "test.md") == 1
    comments = comments_map(doc)
    assert comments.get(orphan) is None
    assert comments.get(reply) is None
    assert comments.get(survivor) is not None

    # Idempotent: nothing left to collect.
    assert gc_orphaned_comments(doc, "test.md") == 0


def test_mutate_doc_persists_comment_to_sidecar_not_md(
    tmp_path: Path, run_async: Callable[[Coroutine[Any, Any, Any]], Any]
) -> None:
    async def run() -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "doc.md").write_text("hello brave new world\n", encoding="utf-8")

        manager = SyncManager(vault)
        await manager.start()
        try:

            def mutate(doc: Doc) -> str:
                text = doc.get("content", type=Text)
                return create_comment(
                    doc,
                    user_id="u1",
                    user_name="Alice",
                    text="via REST",
                    anchor_start=_anchors(text, 6, 15)[0],
                    anchor_end=_anchors(text, 6, 15)[1],
                    parent_id=None,
                )

            cid = await manager.mutate_doc("doc.md", mutate)
            # REST-created room with no clients gets a scheduled cleanup so it doesn't leak.
            assert "doc.md" in manager._cleanup_tasks

            room = await manager._ws_server.get_room("doc.md")  # type: ignore[union-attr]
            await manager._save_room("doc.md", room)

            # Comment persists in the sidecar, .md stays pure markdown.
            assert (vault / "doc.md").read_text(encoding="utf-8") == "hello brave new world\n"
            fresh = Doc()
            state = crdt_store.read_state(vault, "doc.md")
            assert state is not None
            fresh.apply_update(state)
            assert comments_map(fresh).get(cid)["text"] == "via REST"

            with pytest.raises(FileNotFoundError):
                await manager.mutate_doc("missing.md", lambda doc: None)
        finally:
            await manager.stop()

    run_async(run())
