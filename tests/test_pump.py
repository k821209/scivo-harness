"""The stream reader that makes idle-time task notifications visible."""
from __future__ import annotations

import asyncio

import pytest

from scivo_harness.pump import MessagePump, PumpClosed


class _Source:
    """An async iterator fed by hand, like the SDK's receive_messages()."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.q.get()
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item


async def _settle():
    for _ in range(3):
        await asyncio.sleep(0)


def test_idle_messages_are_delivered_at_once_and_turn_messages_to_the_turn():
    async def main():
        src = _Source(); seen = []
        pump = MessagePump(src, seen.append)
        await pump.start()
        src.q.put_nowait("note-while-idle"); await _settle()
        assert seen == ["note-while-idle"] and pump.idle_count == 1
        q = pump.open_turn()
        src.q.put_nowait("stream-1"); src.q.put_nowait("result"); await _settle()
        assert await q.get() == "stream-1" and await q.get() == "result"
        assert seen == ["note-while-idle"]          # nothing leaked to idle during the turn
        src.q.put_nowait("late-note")               # arrives after the result, before close
        await _settle()
        pump.close_turn()
        assert seen == ["note-while-idle", "late-note"]   # handed back to idle time
        await pump.stop()
        assert not pump.alive
    asyncio.run(main())


def test_a_stream_that_ends_mid_turn_fails_the_turn_instead_of_hanging():
    async def main():
        src = _Source(); pump = MessagePump(src, lambda m: None)
        await pump.start()
        q = pump.open_turn()
        src.q.put_nowait(RuntimeError("cli died")); await _settle()
        closed = await asyncio.wait_for(q.get(), timeout=1)
        assert isinstance(closed, PumpClosed) and "cli died" in str(closed.error)
        pump.close_turn()
        await pump.stop()
    asyncio.run(main())


def test_a_display_failure_does_not_kill_the_reader():
    async def main():
        src = _Source(); calls = []

        def on_idle(m):
            calls.append(m)
            raise ValueError("printing broke")

        pump = MessagePump(src, on_idle)
        await pump.start()
        src.q.put_nowait("a"); src.q.put_nowait("b"); await _settle()
        assert calls == ["a", "b"] and pump.alive
        await pump.stop()
    asyncio.run(main())


# ── polling asks only for what is new ────────────────────────────────────────

from scivo_harness import control as _control   # noqa: E402


def test_an_older_mcp_is_detected_from_its_own_error():
    """Every scivo tool refuses arguments it does not know, so a server
    predating `since_seq` fails the whole poll. The fallback has to recognise
    that one failure and no other."""
    rejects = _control._rejects_since_seq
    for error in (
        "Input validation error: since_seq: Unexpected keyword argument",
        "ValidationError: extra fields not permitted (since_seq)",
        "unknown argument 'since_seq'",
    ):
        assert rejects(error), error
    for error in (
        "connection reset by peer",
        "publication not found",
        "",
        None,
        "timed out waiting for since_seq results",   # mentions it, not a rejection
    ):
        assert not rejects(error), error


# ── images in the web view ───────────────────────────────────────────────────
# "웹에서 이미지가 뜰때가 있고 안뜰때가 잇음" — a photograph saved as PNG came
# back as "[… 1638 KB — not inlined for the web view]" while the same picture
# as JPEG appeared (user, 2026-10-10). The format was chosen from the
# filename, so a photo named .png was re-encoded as PNG and stayed huge.

pytest.importorskip("PIL", reason="Pillow is optional; the shrink path needs it")


def _noise_photo(w=2000, h=1400):
    """Gradients plus grain: what a camera or a generated portrait looks like
    to an encoder, and what PNG is worst at."""
    import random
    from PIL import Image
    random.seed(11)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            base = (int(120 + 100 * (x / w)), int(90 + 120 * (y / h)), int(140 - 60 * (x / w)))
            c = tuple(max(0, min(255, v + random.randint(-18, 18))) for v in base)
            for dy in (0, 1):
                for dx in (0, 1):
                    if x + dx < w and y + dy < h:
                        px[x + dx, y + dy] = c
    return img


def _encode(img, fmt):
    import io
    buf = io.BytesIO()
    img.save(buf, fmt, **({"optimize": True} if fmt == "PNG" else {"quality": 92}))
    return buf.getvalue()


def test_a_photograph_fits_whatever_its_filename_says():
    data = _encode(_noise_photo(), "PNG")
    assert len(data) > _control.MAX_IMG_BYTES          # the case that failed
    out, mime, note = _control._shrink_for_web(data, "image/png", ".png")
    assert len(out) <= _control.MAX_IMG_BYTES
    assert mime == "image/jpeg"                        # not PNG, despite the suffix
    assert note is None


def test_transparency_is_kept_when_the_image_fits():
    from PIL import Image
    logo = Image.new("RGBA", (1800, 1200), (0, 0, 0, 0))
    for x in range(200, 1600):
        for y in range(300, 900):
            logo.putpixel((x, y), (20, 90, 200, 255))
    out, mime, _ = _control._shrink_for_web(_encode(logo, "PNG"), "image/png", ".png")
    assert mime == "image/png"
    assert len(out) <= _control.MAX_IMG_BYTES


def test_transparency_is_given_up_rather_than_the_picture():
    """A half-transparent photograph cannot fit as PNG at any step on the ladder.
    Flattened onto white it fits — and a picture that shows beats one that
    does not."""
    photo = _noise_photo().convert("RGBA")
    photo.putalpha(200)
    out, mime, _ = _control._shrink_for_web(_encode(photo, "PNG"), "image/png", ".png")
    assert len(out) <= _control.MAX_IMG_BYTES
    assert mime == "image/jpeg"


def test_the_cap_leaves_room_for_base64_inside_a_firestore_document():
    """An inlined image is base64 in a transcript chunk document, and
    Firestore caps a document at 1 MB. 800 KB of image was 1.07 MB of base64."""
    assert _control.MAX_IMG_BYTES * 4 / 3 < 1_000_000


def test_an_svg_is_passed_through_untouched():
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><circle r='9'/></svg>"
    out, mime, note = _control._shrink_for_web(svg, "image/svg+xml", ".svg")
    assert (out, mime, note) == (svg, "image/svg+xml", None)


# ── messages sent while a turn is running ────────────────────────────────────
# The queue hands out one message per prompt, so a line typed while the session
# was busy waited for the turn AFTER the one it was queued for. The second line
# is usually the correction to the first (user, 2026-10-10):
#   "한계일수도 있어 … 배경이 움직이는 영상은 아니엇거든."   queued
#   "아니지 배경은 움직이는데, 위치가 바뀌는건 아니엇음."      queued

from scivo_harness.control import merge_web_messages   # noqa: E402


def test_two_queued_lines_arrive_as_one_message():
    first = "한계일수도 있어 아까 줬던 프롬프트들은 배경이 움직이는 영상은 아니엇거든."
    second = "아니지 배경은 움직이는데, 위치가 바뀌는건 아니엇음."
    got = merge_web_messages([first, second])
    assert got == f"{first}\n\n{second}"


def test_one_message_is_handed_back_untouched():
    assert merge_web_messages(["only this"]) == "only this"
    one = {"text": "with a picture", "images": ["/tmp/a.png"]}
    assert merge_web_messages([one]) is one
    assert merge_web_messages([]) == ""


def test_images_accumulate_across_the_merged_messages():
    got = merge_web_messages([
        {"text": "look at this", "images": ["/tmp/a.png"]},
        "and compare it to the earlier one",
        {"text": "", "images": ["/tmp/b.png"]},
    ])
    assert got["images"] == ["/tmp/a.png", "/tmp/b.png"]
    assert got["text"] == "look at this\n\nand compare it to the earlier one"


def test_an_empty_line_does_not_leave_a_gap():
    assert merge_web_messages(["first", "   ", "second"]) == "first\n\nsecond"
