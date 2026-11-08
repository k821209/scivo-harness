"""A page that stops updating has to say so somewhere.

`Control.errors` had six writers and no readers: a failed chunk write, a flush
exception, a dead head write and a failed poll all went into a list nobody
looked at. So when the page froze and the terminal carried on as if nothing
were wrong, the session it belonged to could not be asked why (user,
2026-11-08: "scivo 웹채팅에서는 멈�ìì¶ê³  cmd에서는 [정상]").

And `_call` held `self._lock` with no timeout, so one hung MCP call froze the
writer, the poller and the heartbeat together — permanently, and silently.
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest

from scivo_harness import control as control_mod
from scivo_harness.control import Control
from scivo_harness.scivo_mcp import ToolOutcome


def _control(**over):
    """A Control with its machinery in place but nothing wired to a network."""
    c = Control.__new__(Control)
    # `active` is a property reading `_client`, so a stand-in client is what
    # makes it active — and every test here needs one anyway.
    c._client = types.SimpleNamespace(call=None)
    c.events = []
    c.errors = []
    c._dirty = set()
    c._lock = asyncio.Lock()
    c._last_flush = None
    c._stuck_said = False
    c._wake = asyncio.Event()
    c.sid = "s1"
    c.link = types.SimpleNamespace(pub_id="pub1")
    for name, value in over.items():
        setattr(c, name, value)
    return c


def test_health_reports_a_page_that_never_wrote_differently_from_one_that_stopped():
    fresh = _control()
    assert fresh.health()["last_flush_ago"] is None, "never wrote"
    ran = _control(_last_flush=time.monotonic() - 30)
    assert ran.health()["last_flush_ago"] == pytest.approx(30, abs=2)


def test_health_surfaces_the_errors_nobody_could_read():
    c = _control(errors=["write chunk 3: stream closed"], _dirty={3})
    state = c.health()
    assert state["errors"] == 1
    assert state["last_error"] == "write chunk 3: stream closed"
    assert state["pending_chunks"] == 1


def test_a_hung_call_becomes_an_error_and_releases_the_lock():
    """The freeze: `_call` held the lock while awaiting forever, so the writer,
    the poller and the heartbeat all blocked on it and the page died solid."""
    async def hang(*_a, **_k):
        await asyncio.Event().wait()

    c = _control(_client=types.SimpleNamespace(call=hang))

    async def go():
        outcome = await c._call("put_page_data")
        # The lock has to be free afterwards, or the next caller inherits the
        # freeze even though this one gave up.
        assert not c._lock.locked()
        return outcome

    monkey = control_mod.CALL_TIMEOUT
    control_mod.CALL_TIMEOUT = 0.05
    try:
        # The test has its own bound, because the thing under test is a hang:
        # without the timeout in `_call` this HUNG the whole suite instead of
        # failing, which is a worse way to learn the same thing.
        outcome = asyncio.run(asyncio.wait_for(go(), timeout=5))
    except asyncio.TimeoutError:
        raise AssertionError("_call did not give up — the lock is held forever")
    finally:
        control_mod.CALL_TIMEOUT = monkey
    assert isinstance(outcome, ToolOutcome) and not outcome.ok
    assert "no answer" in (outcome.error or "")
    assert c.errors and "no answer" in c.errors[-1]


def test_a_failed_chunk_write_does_not_count_as_a_flush():
    """`last_flush` is "everything got through", not "we tried"."""
    async def refuse(*_a, **_k):
        return ToolOutcome(ok=False, error="stream closed")

    c = _control(_client=types.SimpleNamespace(call=refuse), _dirty={0})
    c.events = [{"i": 0}]
    asyncio.run(c._flush())
    assert c._last_flush is None
    assert c._dirty == {0}, "it has to be retried"
    assert c.errors


def test_a_good_chunk_write_marks_the_flush():
    async def accept(*_a, **_k):
        return ToolOutcome(ok=True)

    c = _control(_client=types.SimpleNamespace(call=accept), _dirty={0})
    c.events = [{"i": 0}]
    asyncio.run(c._flush())
    assert c._last_flush is not None and not c._dirty and not c.errors


def test_the_terminal_is_told_once_per_spell_and_told_when_it_recovers(capsys):
    c = _control(_dirty={0}, _last_flush=time.monotonic() - 999,
                 errors=["write chunk 0: stream closed"])
    c._say_if_stuck()
    first = capsys.readouterr().out
    assert "not keeping up" in first and "stream closed" in first
    assert "terminal is unaffected" in first.lower()

    c._say_if_stuck()
    assert capsys.readouterr().out == "", "not once per cycle — once per spell"

    c._dirty = set()
    c._last_flush = time.monotonic()
    c._say_if_stuck()
    # An all-clear, because a warning that never clears trains people to
    # ignore the next one.
    assert "caught up" in capsys.readouterr().out


def test_a_short_burst_behind_is_not_called_stuck(capsys):
    c = _control(_dirty={0}, _last_flush=time.monotonic() - 1)
    c._say_if_stuck()
    assert capsys.readouterr().out == ""


def test_the_head_carries_when_the_conversation_last_had_anything_in_it():
    """`beat` is the heartbeat loop's clock, not the conversation's.

    It moves every few seconds for as long as the process lives, so a session
    idle since September and one being typed into right now carry beats
    seconds apart — and a list sorted by it cannot separate them, which is
    what was reported (user, 2026-11-08: "소팅은 안되네"). Measured at the
    time: eleven sessions, beats all inside one 6-second window, started
    anywhere from 09-30 to 11-08.
    """
    import asyncio

    from scivo_harness.scivo_mcp import ToolOutcome

    written = {}

    async def capture(name, **kwargs):
        written.update(kwargs.get("data") or {})
        return ToolOutcome(ok=True)

    c = _control(_client=types.SimpleNamespace(call=capture))
    c._rev = 0
    c._started = 1
    c.project_name = "p"
    c.project_id = "pid"
    c.model = "m"
    c.events = [{"i": 0, "t": 1_700_000_000_000},
                {"i": 1, "t": 1_700_000_123_456}]
    asyncio.run(c._write_head(state="live"))
    assert written["last_event_at"] == 1_700_000_123_456, written
    # And it is NOT the heartbeat: the two must be separable.
    assert written["beat"] != written["last_event_at"]


def test_an_empty_conversation_claims_no_activity_time():
    """None, not the heartbeat: a page with nothing said in it must not sort
    as though it had just been used."""
    import asyncio

    from scivo_harness.scivo_mcp import ToolOutcome

    written = {}

    async def capture(name, **kwargs):
        written.update(kwargs.get("data") or {})
        return ToolOutcome(ok=True)

    c = _control(_client=types.SimpleNamespace(call=capture))
    c._rev = 0
    c._started = 1
    c.project_name = "p"
    c.project_id = "pid"
    c.model = "m"
    c.events = []
    asyncio.run(c._write_head(state="live"))
    assert written["last_event_at"] is None
