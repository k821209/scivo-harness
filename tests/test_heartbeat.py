"""The heartbeat beats fast only while someone can see it.

It exists so a session that DIED — killed, the machine gone, no goodbye —
stops looking alive, and five seconds is how fast a person watching should
find out. Nobody watches a session idle for a week, and it wrote 17,280 beats
a day regardless: 22 sessions, 420,000 writes a day, each one also a billed
read for every browser tab subscribed to the head (Cloud Monitoring,
2026-11-10).
"""
from __future__ import annotations

import asyncio
import time

from scivo_harness import control as C
from scivo_harness.config import ScivoConfig
from scivo_harness.scivo_mcp import ToolOutcome


class Heads:
    def __init__(self):
        self.heads: list[tuple[float, dict]] = []

    async def call(self, name, **kw):
        if name == "put_page_data" and kw.get("doc_id") == "head":
            self.heads.append((time.monotonic(), dict(kw["data"])))
        return ToolOutcome(True, items=[{}])


def _control(tmp_path, *, quiet=3600.0):
    ctl = C.Control(ScivoConfig(root=tmp_path, command="x"), "p", "pid", "m")
    ctl._client = Heads()
    ctl.link = C.Link("pub", "url")
    ctl.sid = "s"
    ctl._last_activity = time.monotonic() - quiet
    return ctl


async def _writing(ctl, seconds, during=None):
    task = asyncio.create_task(ctl._writer())
    try:
        if during is not None:
            await during()
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_idle_and_unwatched_is_slow_everything_else_is_as_fast_as_before(tmp_path):
    ctl = _control(tmp_path)
    assert ctl._beat_every() == C.BEAT_IDLE
    ctl._viewer_at = time.monotonic()                 # someone is using the page
    assert ctl._beat_every() == C.BEAT_EVERY == 5.0
    ctl._viewer_at = 0.0
    ctl._last_activity = time.monotonic()             # something is happening
    assert ctl._beat_every() == C.BEAT_EVERY


def test_an_unwatched_session_writes_a_sixtieth_of_the_beats():
    before = 86400 / C.BEAT_EVERY
    after = 86400 / C.BEAT_IDLE
    assert before == 17_280 and after == 288
    assert after == before / 60


def test_an_idle_session_stops_beating_at_the_fast_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "BEAT_EVERY", 0.05)
    monkeypatch.setattr(C, "BEAT_IDLE", 30.0)
    ctl = _control(tmp_path)
    asyncio.run(_writing(ctl, 0.6))
    # Twelve at the fast rate. One is allowed: the write that SAYS it has
    # gone slow.
    assert len(ctl._client.heads) <= 1


def test_the_head_says_which_rhythm_it_is_keeping(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "BEAT_EVERY", 0.05)
    monkeypatch.setattr(C, "BEAT_IDLE", 30.0)
    ctl = _control(tmp_path, quiet=0.0)               # busy
    asyncio.run(ctl._write_head(state="live"))
    assert ctl._client.heads[-1][1]["beat_every"] == 0.05
    ctl._last_activity = time.monotonic() - 3600
    asyncio.run(ctl._write_head(state="live"))
    assert ctl._client.heads[-1][1]["beat_every"] == 30.0


def test_going_quiet_is_said_before_the_fast_promise_runs_out(tmp_path, monkeypatch):
    """A head still promising a beat every five seconds, from a session that
    has gone to five minutes, reads as dead a quarter of a minute later."""
    monkeypatch.setattr(C, "BEAT_EVERY", 0.05)
    monkeypatch.setattr(C, "BEAT_IDLE", 30.0)
    monkeypatch.setattr(C, "ACTIVE_WINDOW", 0.3)
    ctl = _control(tmp_path, quiet=0.0)
    asyncio.run(_writing(ctl, 0.9))
    heads = ctl._client.heads
    fast = [t for t, h in heads if h["beat_every"] == 0.05]
    slow = [t for t, h in heads if h["beat_every"] == 30.0]
    assert fast, "it beat fast while it was busy"
    assert len(slow) == 1, "one write to say it has gone slow, then silence"
    # Said within a couple of fast beats of the last fast one — well inside
    # the two and a half intervals a reader allows.
    assert slow[0] - fast[-1] < 0.05 * 2.5
    assert heads[-1][1]["beat_every"] == 30.0


def test_someone_arriving_gets_a_beat_now_not_in_five_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "BEAT_EVERY", 0.05)
    monkeypatch.setattr(C, "BEAT_IDLE", 30.0)
    ctl = _control(tmp_path)

    async def main():
        arrived = {}

        async def someone_opens_the_page():
            await asyncio.sleep(0.2)
            arrived["at"] = time.monotonic()
            arrived["before"] = len(ctl._client.heads)
            await ctl._intake([{"doc": "control", "reviewer": "owner", "sid": "s",
                                "id": "control", "viewer": 1731229200000}])
        await _writing(ctl, 0.3, during=someone_opens_the_page)
        return arrived
    arrived = asyncio.run(main())
    after = [(t, h) for t, h in ctl._client.heads[arrived["before"]:]]
    assert after, "no beat after the viewer arrived"
    assert after[0][0] - arrived["at"] < 0.15
    assert after[0][1]["beat_every"] == 0.05
    assert len(after) >= 3, "and it keeps the fast rhythm while they are there"


def test_working_in_the_terminal_wakes_the_beat_too(tmp_path, monkeypatch):
    """The Connect list sorts by what the head last said happened."""
    monkeypatch.setattr(C, "BEAT_EVERY", 0.05)
    monkeypatch.setattr(C, "BEAT_IDLE", 30.0)
    ctl = _control(tmp_path)

    async def main():
        async def a_line_is_typed():
            await asyncio.sleep(0.15)
            ctl.user("back at the keyboard", "terminal")
        await _writing(ctl, 0.3, during=a_line_is_typed)
    asyncio.run(main())
    last = ctl._client.heads[-1][1]
    assert last["beat_every"] == 0.05
    assert last["last_event_at"] is not None


def test_the_page_judges_silence_by_that_field():
    """Two halves of one protocol in two languages."""
    page = C.page_html()
    assert "head.beat_every" in page
