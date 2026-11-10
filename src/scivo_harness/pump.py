"""One reader for the SDK stream, always on.

The SDK stream has a single consumer. The REPL used to read it only inside a
turn (`receive_response()`), so a task notification — a Monitor's event, a
background job finishing — that arrived while the prompt was idle waited,
unseen, until the next user message. That made `Monitor` useless for the
thing it exists for: telling you when something happened while you were not
asking. Now one task reads `receive_messages()` for the life of the client
and routes each message: into the open turn's queue while a turn runs, to
`on_idle` (printed at once) otherwise.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any


class PumpClosed:
    """Put into an open turn's queue when the reader ends, so a turn waiting
    on `queue.get()` fails with a reason instead of hanging."""

    def __init__(self, error: BaseException | None) -> None:
        self.error = error

    def __repr__(self) -> str:
        return f"PumpClosed({self.error!r})"


class StreamEnded(RuntimeError):
    """The reader is gone, so nothing sent from here on can be answered.

    Its own type because the remedy is not "report the failed turn and carry
    on": every later turn would fail the same way. The session has to get a
    new reader — which means a new CLI on the same conversation.
    """


class MessagePump:
    def __init__(self, source: AsyncIterator[Any], on_idle: Callable[[Any], None]) -> None:
        self._source = source
        self._on_idle = on_idle
        self._turn: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self.idle_count = 0
        # Remembered, not only delivered. The reader told an OPEN turn that it
        # had ended and told nobody otherwise, so one that died between turns
        # — a message the SDK could not parse, while the CLI itself was fine —
        # left the next turn waiting on a queue nothing would ever fill: a
        # spinner counting minutes, and Esc could not end it either, since the
        # interrupt's answer arrives through the same dead reader.
        self.ended = False
        self.error: BaseException | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._reader(), name="scivo-message-pump")

    async def _reader(self) -> None:
        error: BaseException | None = None
        try:
            async for message in self._source:
                self._route(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the reason reaches the turn
            error = exc
        finally:
            self.ended = True
            self.error = error
            if self._turn is not None:
                self._turn.put_nowait(PumpClosed(error))

    def _route(self, message: Any) -> None:
        if self._turn is not None:
            self._turn.put_nowait(message)
            return
        self.idle_count += 1
        try:
            self._on_idle(message)
        except Exception:  # noqa: BLE001 — a display failure must not kill the reader
            pass

    def open_turn(self) -> asyncio.Queue:
        """Messages go to the returned queue until close_turn()."""
        self._turn = asyncio.Queue()
        if self.ended:
            # Nothing will ever arrive; say so now rather than never.
            self._turn.put_nowait(PumpClosed(self.error))
        return self._turn

    def why_ended(self) -> str:
        """One line for a person: what the reader last said, if anything."""
        if self.error is None:
            return "the stream closed"
        return f"{type(self.error).__name__}: {self.error}"

    def close_turn(self) -> None:
        """Anything still queued after the turn's result belongs to idle time."""
        queue, self._turn = self._turn, None
        if queue is None:
            return
        while not queue.empty():
            message = queue.get_nowait()
            if not isinstance(message, PumpClosed):
                self._route(message)

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None
