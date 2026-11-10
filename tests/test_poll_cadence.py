"""How often the page's inbox is read follows how much the session is used.

Measured 2026-11-10 over 24 h (Cloud Monitoring): 34.1 million Firestore reads
a day with 22 control pages live — median uptime a week, the oldest 41 days —
each reading its inbox twice a second for as long as the process lived. A
poll is six billed reads, so an unattended session cost a million reads a day
to learn that nobody had typed.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from scivo_harness import control as C
from scivo_harness.config import ScivoConfig
from scivo_harness.scivo_mcp import ToolOutcome

READS_PER_POLL = 6        # publication + four fixed documents + one empty query


def test_a_working_session_is_read_as_fast_as_it_ever_was():
    """The other direction first: none of this may slow a session in use."""
    assert C.poll_interval(0) == C.POLL_ACTIVE == 0.15
    assert C.poll_interval(59) == 0.15
    assert C.poll_interval(61) == C.POLL_IDLE == 0.5
    assert C.poll_interval(9 * 60) == 0.5


def test_the_interval_grows_with_the_quiet_and_never_shrinks_with_it():
    quiet = [0, 30, 60, 300, 600, 1800, 3600, 6 * 3600, 30 * 86400]
    intervals = [C.poll_interval(q) for q in quiet]
    assert intervals == sorted(intervals)
    assert C.poll_interval(30 * 86400) == C.POLL_LADDER[-1][1]


def test_an_unattended_session_costs_a_thirtieth_of_what_it_did():
    """The claim, as arithmetic, so a change to the ladder has to face it."""
    before = 86400 / 0.5 * READS_PER_POLL                    # 1,036,800 a day
    after = 86400 / C.poll_interval(7 * 86400) * READS_PER_POLL
    assert before == 1_036_800
    assert after == before / 30
    assert after < 50_000          # one idle session fits Firestore's free daily quota


def test_a_card_waiting_for_a_person_is_no_longer_the_fastest_state():
    """Pending used to mean 0.15 s for as long as the card sat there: forty
    reads a second, overnight, waiting for someone asleep."""
    assert C.poll_interval(8 * 3600, pending=True) == C.PENDING_FLOOR
    assert C.PENDING_FLOOR > C.POLL_IDLE
    # …but a card just posted is answered as fast as anything else.
    assert C.poll_interval(5, pending=True) == C.POLL_ACTIVE


def test_someone_using_the_page_is_not_made_to_wait_and_does_not_buy_full_speed():
    assert C.poll_interval(8 * 3600, viewer=True) == C.VIEWER_FLOOR
    # A tab left open must not hold the session at the working cadence.
    assert C.VIEWER_FLOOR > C.POLL_IDLE
    assert C.poll_interval(5, viewer=True) == C.POLL_ACTIVE


# ── the poller itself ───────────────────────────────────────────────────────

class Inbox:
    def __init__(self):
        self.polls: list[float] = []
        self.items: list = []

    async def call(self, name, **kw):
        if name == "list_responses":
            self.polls.append(time.monotonic())
            return ToolOutcome(True, items=list(self.items))
        return ToolOutcome(True, items=[{}])


def _control(tmp_path, monkeypatch, ladder):
    monkeypatch.setattr(C, "POLL_LADDER", ladder)
    ctl = C.Control(ScivoConfig(root=tmp_path, command="x"), "p", "pid", "m")
    ctl._client = Inbox()
    ctl.link = C.Link("pub", "url")
    ctl.sid = "s"
    return ctl


def test_an_idle_poller_reads_at_the_slow_rung(tmp_path, monkeypatch):
    async def main():
        ctl = _control(tmp_path, monkeypatch, ((0.0, 0.01), (0.05, 0.2)))
        ctl._last_activity = time.monotonic() - 10          # long quiet
        task = asyncio.create_task(ctl._poller())
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return ctl
    ctl = asyncio.run(main())
    # 0.5 s at one read per 0.2 s: two, not the fifty the fast rung would make.
    assert 1 <= len(ctl._client.polls) <= 3
    assert ctl._poll_s == 0.2


def test_local_activity_cuts_a_long_sleep_short(tmp_path, monkeypatch):
    """The interval is chosen when the sleep starts. A session that wakes —
    a line typed in the terminal, an approval card posted — must not stay
    deaf to the page for the rest of a nap it began while idle."""
    async def main():
        ctl = _control(tmp_path, monkeypatch, ((0.0, 0.01), (0.05, 30.0)))
        ctl._last_activity = time.monotonic() - 10
        task = asyncio.create_task(ctl._poller())
        await asyncio.sleep(0.1)
        asleep = len(ctl._client.polls)
        ctl.status("back at the keyboard")                  # any outbound event
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return asleep, len(ctl._client.polls)
    asleep, awake = asyncio.run(main())
    assert asleep == 0, "the 30 s rung should not have read yet"
    assert awake >= 1, "activity should have brought the next read forward"


def test_a_viewer_mark_from_the_page_speeds_the_next_reads(tmp_path, monkeypatch):
    async def main():
        ctl = _control(tmp_path, monkeypatch, ((0.0, 0.01), (0.05, 30.0)))
        monkeypatch.setattr(C, "VIEWER_FLOOR", 0.02)
        ctl._last_activity = time.monotonic() - 10
        await ctl._intake([{"doc": "control", "reviewer": "owner", "sid": "s",
                            "id": "control", "viewer": 1731229200000}])
        first = ctl._next_poll()
        # The same mark again is not news: presence is a CHANGE of the mark.
        seen = ctl._viewer_at
        await ctl._intake([{"doc": "control", "reviewer": "owner", "sid": "s",
                            "id": "control", "viewer": 1731229200000}])
        return ctl, first, seen
    ctl, first, seen = asyncio.run(main())
    assert first == 0.02
    assert ctl._viewer_at == seen


def test_a_mark_is_judged_on_this_clock_not_the_browsers(tmp_path, monkeypatch):
    """A phone a year out — either way — is present when its mark changes and
    absent when it stops changing, like any other."""
    async def main():
        ctl = _control(tmp_path, monkeypatch, ((0.0, 0.01), (0.05, 30.0)))
        ctl._last_activity = time.monotonic() - 10
        for wrong in (1, 4102444800000):                    # 1970, and 2100
            await ctl._intake([{"doc": "control", "reviewer": "owner", "sid": "s",
                                "id": "control", "viewer": wrong}])
            assert ctl._next_poll() == C.VIEWER_FLOOR
        ctl._viewer_at -= C.VIEWER_WINDOW + 1               # …and then it goes quiet
        return ctl._next_poll()
    assert asyncio.run(main()) == 30.0


def test_a_mark_from_someone_else_or_another_session_is_not_presence(tmp_path, monkeypatch):
    async def main():
        ctl = _control(tmp_path, monkeypatch, ((0.0, 0.01), (0.05, 30.0)))
        ctl._last_activity = time.monotonic() - 10
        await ctl._intake([
            {"doc": "control", "reviewer": "Reviewer A", "sid": "s", "id": "control", "viewer": 5},
            {"doc": "control", "reviewer": "owner", "sid": "an-earlier-session", "id": "control", "viewer": 6},
        ])
        return ctl._next_poll()
    assert asyncio.run(main()) == 30.0


def test_the_page_is_told_the_interval(tmp_path, monkeypatch):
    """So "sending…" can say what it is waiting for."""
    written = {}

    class Head(Inbox):
        async def call(self, name, **kw):
            if name == "put_page_data" and kw.get("doc_id") == "head":
                written.update(kw["data"])
            return await super().call(name, **kw)

    async def main():
        ctl = _control(tmp_path, monkeypatch, C.POLL_LADDER)
        ctl._client = Head()
        ctl._last_activity = time.monotonic() - 2 * 3600
        ctl._next_poll()
        await ctl._write_head(state="live")
    asyncio.run(main())
    assert written["poll_s"] == C.POLL_LADDER[-1][1]


def test_the_page_reads_that_field_and_writes_the_mark_this_side_reads():
    """Two halves of one protocol in two languages; a rename on either side
    would pass every test of its own."""
    page = C.page_html()
    assert "poll_s" in page
    assert 'C.put("responses", "control", { doc: "control", sid: S.sid, viewer: now })' in page
