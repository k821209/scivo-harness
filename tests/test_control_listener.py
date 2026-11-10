"""The session is TOLD when the page writes, and checks the thing telling it.

Asking was the bill: six Firestore reads a time, twice a second per session,
for as long as the process lived — 34.1 million reads a day across 22
sessions (2026-11-10). An MCP with `wait_for_responses` holds a listener open
instead. Everything here is about the two ways that can go wrong: the
listener is not there (an older MCP), or it is there and has gone quiet.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from scivo_harness import control as C
from scivo_harness import toolsets
from scivo_harness.config import ScivoConfig
from scivo_harness.scivo_mcp import ToolOutcome


class Page:
    """An MCP with a listener: `write()` is the page writing a document."""

    def __init__(self, *, has_listener=True):
        self.has_listener = has_listener
        self.docs: dict[str, dict] = {}
        self.version = 0
        self.seen: dict[str, int] = {}
        self.changed = asyncio.Event()
        self.reads = 0                      # list_responses calls: the billed kind
        self.waits = 0
        self.listening = True               # False: the stream stopped delivering
        self.deaf = False                   # True: says listening, delivers nothing
        self.hang = False
        self.puts: list[float] = []

    def write(self, doc_id: str, **data) -> None:
        self.docs[doc_id] = {**self.docs.get(doc_id, {}), **data,
                             "reviewer": "owner", "sid": "s"}
        self.version += 1
        self.seen[doc_id] = self.version
        self.changed.set()

    async def call(self, name, **kw):
        if name == "list_responses":
            self.reads += 1
            return ToolOutcome(True, items=[{"id": k, **v} for k, v in self.docs.items()])
        if name == "put_page_data":
            self.puts.append(time.monotonic())
            return ToolOutcome(True, items=[{}])
        if name != "wait_for_responses":
            return ToolOutcome(True, items=[{}])
        if not self.has_listener:
            return ToolOutcome(False, error="Unknown tool: wait_for_responses")
        self.waits += 1
        if self.hang:
            await asyncio.sleep(3600)
        after = int(str(kw.get("cursor") or "0:0").partition(":")[2] or 0)

        def rows():
            if self.deaf:
                return []
            return [{"id": k, **self.docs[k]} for k, v in self.seen.items() if v > after]

        if not self.listening:
            return ToolOutcome(True, items=[{"rows": [], "cursor": kw.get("cursor") or "",
                                             "listening": False, "why": "the listener stopped delivering"}])
        deadline = time.monotonic() + float(kw.get("timeout_s") or 0)
        while not rows() and time.monotonic() < deadline and self.listening:
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=min(0.05, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                pass
        reply = {"rows": rows(), "cursor": f"e:{self.version}", "listening": self.listening}
        if not self.listening:              # the real server always says why
            reply["why"] = "the listener stopped delivering"
        return ToolOutcome(True, items=[reply])


def _control(tmp_path, monkeypatch, page, *, wait=0.2):
    monkeypatch.setattr(C, "LISTEN_WAIT", wait)
    monkeypatch.setattr(C, "LISTEN_STALE", wait + 0.3)
    ctl = C.Control(ScivoConfig(root=tmp_path, command="x"), "p", "pid", "m")
    ctl._client = page
    ctl.link = C.Link("pub", "url")
    ctl.sid = "s"
    ctl._last_activity = time.monotonic()
    return ctl


async def _run(ctl, body, *, also_poller=True):
    tasks = [asyncio.create_task(ctl._listener())]
    if also_poller:
        tasks.append(asyncio.create_task(ctl._poller()))
    try:
        return await body()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_a_message_arrives_when_it_is_written_not_at_the_next_tick(tmp_path, monkeypatch):
    page = Page()

    async def main():
        ctl = _control(tmp_path, monkeypatch, page, wait=5)
        # The reader alone would not look for a quarter of an hour.
        monkeypatch.setattr(C, "POLL_LADDER", ((0.0, 900.0),))

        async def body():
            await asyncio.sleep(0.1)
            sent = time.monotonic()
            page.write("inbox-1", doc="inbox", id="inbox-1", seq=1, text="hello from the web")
            got = await asyncio.wait_for(ctl.messages.get(), timeout=2)
            return got, time.monotonic() - sent
        return await _run(ctl, body)
    got, took = asyncio.run(main())
    assert got == "hello from the web"
    assert took < 0.5


def test_while_it_listens_the_inbox_is_barely_read(tmp_path, monkeypatch):
    """The bill. Half a second on the old cadence is a read every 150 ms."""
    page = Page()

    async def main():
        ctl = _control(tmp_path, monkeypatch, page)

        async def body():
            await asyncio.sleep(0.6)
            return ctl
        return await _run(ctl, body)
    ctl = asyncio.run(main())
    assert ctl.listening
    assert page.reads <= 1, "one read at start-up at most; then only the ten-minute check"
    assert ctl._next_poll() == C.RECONCILE_EVERY
    assert ctl._poll_s == 0.0                    # the page is told nothing waits


def test_an_older_mcp_keeps_the_timer_and_says_why(tmp_path, monkeypatch, capsys):
    page = Page(has_listener=False)

    async def main():
        ctl = _control(tmp_path, monkeypatch, page)
        monkeypatch.setattr(C, "POLL_LADDER", ((0.0, 0.05),))

        async def body():
            await asyncio.sleep(0.4)
            return ctl
        return await _run(ctl, body)
    ctl = asyncio.run(main())
    assert not ctl.listening and ctl._listen_supported is False
    assert page.waits == 0 or page.reads >= 3    # it reads on the timer
    assert page.reads >= 3
    said = capsys.readouterr().out
    assert said.count("no wait_for_responses") == 1 and "scivo update" in said
    health = ctl.health()
    assert health["inbox"].startswith("read every") and "scivo update" in health["inbox_why"]


def test_a_listener_that_says_it_stopped_hands_back_to_the_timer_at_once(tmp_path, monkeypatch):
    """`listening: false` means an empty answer is not an answer. The reader
    was asleep for ten minutes on the listener's word; it must not finish
    that sleep."""
    page = Page()

    async def main():
        ctl = _control(tmp_path, monkeypatch, page)
        monkeypatch.setattr(C, "POLL_LADDER", ((0.0, 0.05),))

        async def body():
            await asyncio.sleep(0.3)
            assert ctl.listening
            before = page.reads
            page.listening = False
            page.changed.set()
            await asyncio.sleep(0.5)
            lost = (ctl.listening, page.reads - before, ctl.health()["inbox_why"])
            page.listening = True               # …and it comes back
            await asyncio.sleep(2.5)
            return lost, ctl.listening
        return await _run(ctl, body)
    (listening, reads, why), back = asyncio.run(main())
    assert listening is False
    assert reads >= 3, "the timer took over"
    assert "stopped delivering" in why
    assert back is True


def test_a_listener_that_never_answers_is_not_believed(tmp_path, monkeypatch):
    page = Page()

    async def main():
        ctl = _control(tmp_path, monkeypatch, page, wait=0.1)
        monkeypatch.setattr(C, "POLL_LADDER", ((0.0, 0.05),))
        real_wait_for = asyncio.wait_for

        async def impatient(awaitable, timeout=None):
            return await real_wait_for(awaitable, timeout=min(timeout or 1, 0.3))

        monkeypatch.setattr(C.asyncio, "wait_for", impatient)

        async def body():
            await asyncio.sleep(0.25)
            page.hang = True
            await asyncio.sleep(1.2)
            return ctl.listening, page.reads
        return await _run(ctl, body)
    listening, reads = asyncio.run(main())
    assert listening is False
    assert reads >= 3


def test_the_reader_checks_the_listener_and_stops_believing_one_that_misses(tmp_path, monkeypatch, capsys):
    """Deaf but cheerful: says it is listening, delivers nothing. Only a
    second path can catch that, which is why the timer never stops entirely."""
    page = Page()
    page.deaf = True

    async def main():
        ctl = _control(tmp_path, monkeypatch, page)
        monkeypatch.setattr(C, "RECONCILE_EVERY", 0.15)

        async def body():
            await asyncio.sleep(0.45)            # its first (empty) answer takes a whole wait
            assert ctl.listening
            got = []
            for n in (1, 2):
                page.write(f"inbox-{n}", doc="inbox", id=f"inbox-{n}", seq=n, text=f"message {n}")
                got.append(await asyncio.wait_for(ctl.messages.get(), timeout=2))
                await asyncio.sleep(0.05)
            return got, ctl
        return await _run(ctl, body)
    got, ctl = asyncio.run(main())
    assert got == ["message 1", "message 2"], "the check is also the safety net"
    assert ctl._listen_trusted is False and not ctl.listening
    assert "missed changes" in ctl.health()["inbox_why"]
    assert "read on a timer" in capsys.readouterr().out
    assert any("had not delivered" in line for line in ctl.errors)


def test_one_miss_is_not_enough_to_drop_it(tmp_path, monkeypatch):
    """A write landing in the same instant as the check is not a broken
    listener, and the timer is the expensive path."""
    page = Page()

    async def main():
        ctl = _control(tmp_path, monkeypatch, page)
        ctl._listen_at = time.monotonic()
        page.docs["inbox-1"] = {"doc": "inbox", "id": "inbox-1", "seq": 1, "text": "x",
                                "reviewer": "owner", "sid": "s"}
        monkeypatch.setattr(C, "RECONCILE_EVERY", 0.05)
        task = asyncio.create_task(ctl._poller())
        await asyncio.wait_for(ctl.messages.get(), timeout=2)
        ctl._listen_at = time.monotonic()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return ctl
    ctl = asyncio.run(main())
    assert ctl._listen_misses == 1 and ctl._listen_trusted is True


def test_the_open_wait_does_not_hold_up_writes_to_the_page(tmp_path, monkeypatch):
    """`_call` serialises every call on one lock. The listener's call is open
    nearly all the time; through that lock the transcript would move once
    every twenty-five seconds."""
    page = Page()

    async def main():
        ctl = _control(tmp_path, monkeypatch, page, wait=2.0)

        async def body():
            await asyncio.sleep(0.1)             # the wait is open now
            started = time.monotonic()
            ctl.user("typed in the terminal", "terminal")
            await ctl._flush()
            return time.monotonic() - started
        return await _run(ctl, body, also_poller=False)
    assert asyncio.run(main()) < 0.5
    assert page.puts


def test_approvals_and_stop_arrive_the_same_way(tmp_path, monkeypatch):
    page = Page()

    async def main():
        ctl = _control(tmp_path, monkeypatch, page, wait=5)
        monkeypatch.setattr(C, "POLL_LADDER", ((0.0, 900.0),))

        async def body():
            asking = asyncio.create_task(ctl.ask_approval("Bash", "ls", False))
            await asyncio.sleep(0.1)
            rid = next(iter(ctl._approvals))
            page.write("approvals", doc="approvals", decisions={rid: "allow"})
            decision = await asyncio.wait_for(asking, timeout=2)
            page.write("control", doc="control", interrupt=1)
            await asyncio.wait_for(ctl.interrupt_requested.wait(), timeout=2)
            return decision
        return await _run(ctl, body)
    assert asyncio.run(main()) == "allow"


def test_an_unpublished_page_is_not_mistaken_for_an_mcp_without_the_tool():
    assert C._is_unknown_tool("Unknown tool: wait_for_responses")
    assert not C._is_unknown_tool("Error executing tool wait_for_responses: publication 'x' not found")
    assert not C._is_unknown_tool("")


def test_the_model_never_sees_the_harness_own_plumbing():
    names = ["wait_for_responses", "list_responses", "get_section", "whoami"]
    for profile in toolsets.PROFILES:
        plan = toolsets.plan(names, profile=profile)
        assert "wait_for_responses" not in plan.kept, profile
        assert "wait_for_responses" in plan.dropped, profile
