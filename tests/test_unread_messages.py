"""What was typed for a session that never read it is not thrown away.

The user, 2026-11-18, of the night their session stopped: "그때 채팅에 입력했던
것들 기록도 다 사라짐. 물론 내 쪽 입력이지만 처리가 안 됐음." The messages sat
in the page's inbox under the stopped session's id. The session that was
started next cleared "previous sessions" — the inbox with them — before
anything had looked at what was in it.
"""
from __future__ import annotations

import asyncio

from scivo_harness import control as C
from scivo_harness.scivo_mcp import ToolOutcome
from tests.test_page_lease import world  # noqa: F401  (the fixture)


def _inbox(store, sid, ident, text, seq, **more):
    store.docs.setdefault("responses", {})[f"inbox-{ident}"] = {
        "doc": "inbox", "sid": sid, "id": ident, "seq": seq, "text": text,
        "reviewer": C.OWNER, **more}


def test_messages_the_stopped_session_never_took_are_handed_to_the_next_one(world):
    store, boot = world

    async def main():
        first = await boot()
        first.user("정리해줘", "web")                      # taken: it is in the session's own log
        first.user("터미널에서 친 말", "terminal")
        await first._flush()
        _inbox(store, first.sid, "a", "정리해줘", 1000)
        # ... and then it stopped. These two were typed at it afterwards.
        _inbox(store, first.sid, "b", "파일 올렸어. 이어서 해줘", 2000)
        _inbox(store, first.sid, "c", "아직 안 됐어?", 3000, images=["data:image/png;base64,AAAA"])
        store.docs["lease"]["owner"]["at"] = 1          # long past its renewal: nobody's
        second = await boot()
        return first, second
    first, second = asyncio.run(main())
    assert [m["text"] for m in second.unread] == ["파일 올렸어. 이어서 해줘", "아직 안 됐어?"]
    assert [m["at"] for m in second.unread] == [2000, 3000]
    assert second.unread[1]["images"] == 1 and "images" not in second.unread[0]
    assert second.unread_known is True
    # And the tidy-up still happened: the old inbox is not left to pile up.
    assert not [k for k in store.docs["responses"] if k.startswith("inbox-")]


def test_the_same_words_sent_twice_and_taken_once_leave_one_unread(world):
    store, boot = world

    async def main():
        first = await boot()
        first.user("계속", "web")
        await first._flush()
        _inbox(store, first.sid, "a", "계속", 1000)
        _inbox(store, first.sid, "b", "계속", 2000)
        store.docs["lease"]["owner"]["at"] = 1
        return await boot()
    second = asyncio.run(main())
    assert [(m["text"], m["at"]) for m in second.unread] == [("계속", 2000)]


def test_nothing_unread_is_nothing_said_and_costs_no_reading_of_the_log(world):
    store, boot = world
    reads = []
    original = store.call

    def call(name, **kw):
        if name == "list_responses":
            reads.append(kw.get("collection", "responses"))
        return original(name, **kw)
    store.call = call

    async def main():
        first = await boot()
        first.user("정리해줘", "web")
        await first._flush()
        store.docs["lease"]["owner"]["at"] = 1
        reads.clear()
        return await boot()
    second = asyncio.run(main())
    assert second.unread == []
    assert "content" not in reads, "the earlier log is read only when there is a message to place"


def test_when_the_earlier_log_cannot_be_read_the_messages_are_kept_and_it_says_so(world):
    store, boot = world
    original = store.call

    def call(name, **kw):
        if name == "list_responses" and kw.get("collection") == "content":
            return ToolOutcome(False, error="deadline exceeded")
        return original(name, **kw)

    async def main():
        first = await boot()
        first.user("정리해줘", "web")
        await first._flush()
        _inbox(store, first.sid, "a", "정리해줘", 1000)
        _inbox(store, first.sid, "b", "이어서", 2000)
        store.docs["lease"]["owner"]["at"] = 1
        store.call = call
        return await boot()
    second = asyncio.run(main())
    assert [m["text"] for m in second.unread] == ["정리해줘", "이어서"], "kept, all of them"
    assert second.unread_known is False, "and not presented as certainly unread"


def test_only_the_owners_messages_with_something_in_them_and_not_this_sessions_own(world):
    store, boot = world

    async def main():
        first = await boot()
        await first._flush()
        _inbox(store, first.sid, "a", "남의 글", 1000, reviewer="someone-else")
        _inbox(store, first.sid, "b", "   ", 2000)
        _inbox(store, first.sid, "c", "진짜 글", 3000)
        store.docs["lease"]["owner"]["at"] = 1
        second = await boot()
        return second
    second = asyncio.run(main())
    assert [m["text"] for m in second.unread] == ["진짜 글"]


def test_the_next_session_puts_them_on_the_page_as_its_first_word(world):
    store, boot = world

    async def main():
        first = await boot()
        await first._flush()
        _inbox(store, first.sid, "b", "파일 올렸어. 이어서 해줘", 2000)
        store.docs["lease"]["owner"]["at"] = 1
        second = await boot()
        second.post_unread()
        return second
    second = asyncio.run(main())
    (event,) = [e for e in second.events if e["kind"] == "unread"]
    assert event["items"] == [{"text": "파일 올렸어. 이어서 해줘", "at": 2000}]
    assert event["known"] is True
    # A tab still running an older page draws an unknown kind as its `text`.
    assert "never read" in event["text"] and "파일 올렸어. 이어서 해줘" in event["text"]
    assert "not run" in event["text"].lower() or "nothing" in event["text"].lower()


def test_a_long_backlog_is_capped_and_says_how_many_were_left_out(world):
    store, boot = world

    async def main():
        first = await boot()
        await first._flush()
        for n in range(C.UNREAD_MAX + 7):
            _inbox(store, first.sid, f"m{n:03d}", f"message {n}", 1000 + n)
        _inbox(store, first.sid, "big", "x" * (C.UNREAD_TEXT + 500), 999_999)
        store.docs["lease"]["owner"]["at"] = 1
        second = await boot()
        second.post_unread()
        return second
    second = asyncio.run(main())
    (event,) = [e for e in second.events if e["kind"] == "unread"]
    assert len(event["items"]) == C.UNREAD_MAX
    assert event["left_out"] == 8
    assert event["items"][-1]["text"].endswith("…") and len(event["items"][-1]["text"]) <= C.UNREAD_TEXT + 1
    assert event["items"][-1]["at"] == 999_999, "the most recent are the ones kept"


def test_start_posts_them_right_after_it_connects_and_the_terminal_is_told_too():
    import inspect
    from scivo_harness.repl import Repl
    start = inspect.getsource(C.Control.start)
    assert start.index('self.status("connected') < start.index("self.post_unread()")
    ensure = inspect.getsource(C.Control._ensure_publication)
    assert ensure.index("await self._collect_unread(link)") < ensure.index("await self._clear_previous_sessions(link)")
    said = inspect.getsource(Repl._control)
    assert "if self.control.unread:" in said and "They were not run." in said
