"""The stream reader that makes idle-time task notifications visible."""
from __future__ import annotations

import asyncio

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
