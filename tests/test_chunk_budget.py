"""A transcript chunk handed to the network is under the document limit. Always.

Firestore refuses a document over 1,048,576 bytes. The caps were per image
(500 KB) and per message (1.5 MB); the thing with the limit is the DOCUMENT,
twenty events sharing it. Two pictures in one reply — each under its own cap —
made a 1.2 MB chunk that was refused, retried for the rest of the session, and
took the writer's backoff to its 30-second ceiling for every later update,
because that backoff only resets when nothing is waiting (review 2026-11-10,
reproduced against the real module before any of this was written).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os

import pytest

from scivo_harness import control as C
from scivo_harness.config import ScivoConfig
from scivo_harness.scivo_mcp import ToolOutcome

FIRESTORE_LIMIT = 1_048_576

needs_pillow = pytest.mark.skipif(importlib.util.find_spec("PIL") is None,
                                  reason="downscaling needs Pillow")


class Firestore:
    """Refuses what the real one refuses, and remembers what it was sent."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.refused: list[int] = []

    async def call(self, name, **kw):
        if name == "put_page_data":
            size = len(json.dumps(kw["data"], ensure_ascii=False).encode("utf-8"))
            if size > FIRESTORE_LIMIT:
                self.refused.append(size)
                return ToolOutcome(False, error=f"400 document too large ({size})")
            self.docs[kw["doc_id"]] = json.loads(json.dumps(kw["data"]))
        return ToolOutcome(True, items=[{}])


def _control(root):
    ctl = C.Control(ScivoConfig(root=root, command="x"), "p", "pid", "m")
    ctl._client = Firestore()
    ctl.link = C.Link("pub", "url")
    ctl.sid = "s"
    return ctl


def _opaque(path, kb):
    """Bytes no encoder can shrink and Pillow cannot open: inlined as they are
    or not at all, with or without Pillow installed."""
    path.write_bytes(b"\xff\xd8" + os.urandom(kb * 1000))
    return path


def test_two_pictures_in_one_reply_still_leave_a_writable_chunk(tmp_path):
    one, two = _opaque(tmp_path / "fig1.jpg", 450), _opaque(tmp_path / "fig2.jpg", 450)
    ctl = _control(tmp_path)
    ctl.user("show me both figures", "terminal")
    ctl.delta(f"Here they are.\n\n![]({one})\n\n![]({two})\n")
    ctl.end_assistant()
    asyncio.run(ctl._flush())

    assert ctl._client.refused == []
    assert not ctl._dirty, "nothing may be left retrying"
    text = ctl._client.docs["log-s-0000"]["events"][1]["text"]
    # The first is there. The second kept its reference and says what took
    # the room — NOT that it was too big, which it was not.
    assert text.count("data:image/") == 1
    assert f"![]({two})" in text
    assert "already holds" in text and "ask for it again" in text


def test_what_follows_in_the_same_chunk_reaches_the_page(tmp_path):
    """The symptom itself: everything after the pictures went missing."""
    ctl = _control(tmp_path)
    ctl.delta(f"![]({_opaque(tmp_path / 'a.jpg', 450)}) ![]({_opaque(tmp_path / 'b.jpg', 450)})")
    ctl.end_assistant()
    ctl.delta("And the next sentence of the reply.")
    ctl.end_assistant()
    asyncio.run(ctl._flush())
    events = ctl._client.docs["log-s-0000"]["events"]
    assert events[-1]["text"] == "And the next sentence of the reply."


@needs_pillow
def test_a_second_picture_arrives_smaller_rather_than_not_at_all(tmp_path):
    import random
    from PIL import Image

    def photo(path, seed):
        random.seed(seed)
        side = 900
        Image.frombytes("RGB", (side, side), random.randbytes(side * side * 3)).save(
            path, "JPEG", quality=70)
        return path

    one, two = photo(tmp_path / "p1.jpg", 1), photo(tmp_path / "p2.jpg", 2)
    assert one.stat().st_size + two.stat().st_size > C.CHUNK_DOC_BUDGET * 3 // 4
    ctl = _control(tmp_path)
    ctl.delta(f"![]({one})\n\n![]({two})")
    ctl.end_assistant()
    asyncio.run(ctl._flush())
    text = ctl._client.docs["log-s-0000"]["events"][0]["text"]
    assert text.count("data:image/") == 2
    assert ctl._client.refused == []


def test_room_spent_by_later_events_is_taken_back_at_the_flush(tmp_path):
    """Inlining leaves room; what comes next into the same twenty events
    spends it. The last word is at the one place every chunk passes through."""
    ctl = _control(tmp_path)
    ctl.delta(f"![plot]({_opaque(tmp_path / 'plot.jpg', 400)})")
    ctl.end_assistant()
    for _ in range(3):                      # three long Korean replies: ~1.2 MB
        ctl.note("가" * 140_000)
    assert ctl._chunk_bytes(0) > FIRESTORE_LIMIT
    asyncio.run(ctl._flush())

    assert ctl._client.refused == [] and not ctl._dirty
    assert ctl._chunk_bytes(0) <= C.CHUNK_DOC_BUDGET
    first = ctl._client.docs["log-s-0000"]["events"][0]["text"]
    # Cut, and SAID so in the event: the page must not silently show less
    # than the terminal did.
    assert "data:image/" not in first
    assert "plot" in first and "not shown on the web view" in first
    # …and somewhere a person can ask about it.
    assert any("cut:" in line for line in ctl.errors)
    assert ctl.health()["errors"] >= 1


def test_text_alone_can_outgrow_a_document_and_is_truncated(tmp_path):
    ctl = _control(tmp_path)
    for _ in range(4):
        ctl.note("나" * 140_000)            # no picture anywhere to cut
    asyncio.run(ctl._flush())
    assert ctl._client.refused == [] and not ctl._dirty
    texts = [e["text"] for e in ctl._client.docs["log-s-0000"]["events"]]
    assert any(t.endswith("…[truncated for the web view]") for t in texts)
    # Cut on a character boundary: Korean is three bytes a character.
    for t in texts:
        t.encode("utf-8")


def test_a_picture_pasted_from_the_page_is_counted_too(tmp_path):
    ctl = _control(tmp_path)
    big = "data:image/png;base64," + "A" * 600_000
    ctl.user("이 그림 봐줘", "web", [big])
    ctl.user("이것도", "web", [big])
    asyncio.run(ctl._flush())
    assert ctl._client.refused == [] and not ctl._dirty
    events = ctl._client.docs["log-s-0000"]["events"]
    assert sum(len(e.get("images") or []) for e in events) == 1
    assert any("pasted image is not shown here" in e["text"] for e in events)


def test_a_chunk_that_fits_is_left_exactly_as_it_is(tmp_path):
    """The other direction: a guard that trims what already fits would be a
    page that quietly shows less, every time."""
    ctl = _control(tmp_path)
    ctl.user("짧은 질문", "terminal")
    ctl.delta(f"![]({_opaque(tmp_path / 'small.jpg', 200)}) 그리고 설명")
    ctl.end_assistant()
    before = json.dumps(ctl.events, ensure_ascii=False)
    assert ctl._fit_chunk(0) == []
    assert json.dumps(ctl.events, ensure_ascii=False) == before
    assert ctl.errors == []
    assert "data:image/" in ctl.events[1]["text"]


def test_the_budget_is_under_the_limit_it_guards():
    assert C.CHUNK_DOC_BUDGET < FIRESTORE_LIMIT
    # One image at its own cap must fit an otherwise empty chunk, or the
    # per-image cap would be a promise the document cannot keep.
    assert C.MAX_IMG_BYTES * 4 / 3 + 2_000 < C.CHUNK_DOC_BUDGET
