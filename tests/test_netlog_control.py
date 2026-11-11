"""The control page's own traffic is in the network log.

`~/.co-scientist/net.log` exists to line a dropped link up against everything
that was in flight. It had the MCP's SSH calls and one harness poll; the
control channel — twice a second per session — was the busiest periodic
thing a session did and the one thing the log could not show (review
2026-11-10).
"""
from __future__ import annotations

import asyncio

from scivo_harness import control as C
from scivo_harness import netlog
from scivo_harness.config import ScivoConfig
from scivo_harness.scivo_mcp import ToolOutcome


def _tally(every=3600.0):
    lines = []
    tally = netlog.Tally(every=every, sink=lambda channel, **kw: lines.append((channel, kw)))
    return tally, lines


def test_ordinary_calls_are_counted_not_written_one_by_one():
    """Ten writes a second while a reply streams would bury the file."""
    tally, lines = _tally()
    for _ in range(200):
        tally.add("page-chunk", 40, True)
    assert lines == []
    tally.flush()
    assert len(lines) == 1
    channel, line = lines[0]
    assert channel == "mcp" and line["kind"] == "page-chunk" and line["host"] == "firestore"
    assert line["note"] == "n=200 max=40ms" and line["ms"] == 40


def test_a_failure_or_a_slow_call_gets_its_own_line_at_once():
    """Those are what a dropped link produces, and why anyone opens the file."""
    tally, lines = _tally()
    tally.add("page-read", 30, True)
    tally.add("page-head", 45_000, False, note="no answer in 45s")
    tally.add("page-chunk", netlog.SLOW_MS + 1, True)
    assert [(l[1]["kind"], l[1]["rc"]) for l in lines] == [("page-head", 1), ("page-chunk", 0)]
    assert lines[0][1]["note"] == "no answer in 45s" and lines[1][1]["note"] == "slow"


def test_the_summary_is_written_on_the_clock():
    tally, lines = _tally(every=0.0)
    tally.add("page-read", 30, True)
    assert len(lines) == 1 and lines[0][1]["note"].startswith("n=1")


def test_what_is_recorded_names_the_purpose_and_never_an_id():
    kinds = {
        C._net_kind("put_page_data", {"doc_id": "log-20261111-101010-abc123-0007"}): "page-chunk",
        C._net_kind("put_page_data", {"doc_id": "head"}): "page-head",
        C._net_kind("put_page_data", {"doc_id": "commands-20261111-101010-abc123"}): "page-commands",
        C._net_kind("put_page_data", {"doc_id": "owner"}): "page-owner",
        C._net_kind("list_responses", {"pub_id": "9f8e7d"}): "page-read",
        C._net_kind("wait_for_responses", {}): "page-listen",
    }
    for got, want in kinds.items():
        assert got == want
        assert "abc123" not in got and "9f8e7d" not in got and len(got) <= 20


def test_every_call_on_the_channel_goes_through_it(tmp_path):
    class Client:
        async def call(self, name, **kw):
            return ToolOutcome(name != "list_responses", items=[{}], error="boom")

    seen = []
    ctl = C.Control(ScivoConfig(root=tmp_path, command="x"), "p", "pid", "m")
    ctl._client, ctl.link, ctl.sid = Client(), C.Link("pub", "url"), "s"
    ctl._net = netlog.Tally(sink=lambda channel, **kw: seen.append(kw))

    async def main():
        await ctl._write_head(state="live")
        ctl.user("hello", "terminal")
        await ctl._flush()
        await ctl._call("list_responses", pub_id="pub")
    asyncio.run(main())
    assert [line["kind"] for line in seen] == ["page-read"]         # the failure, at once
    ctl._net.flush()
    assert {line["kind"] for line in seen} == {"page-read", "page-head", "page-chunk"}


def test_a_log_that_cannot_be_written_does_not_take_the_channel_down(tmp_path):
    class Client:
        async def call(self, name, **kw):
            return ToolOutcome(True, items=[{}])

    def broken(*a, **kw):
        raise OSError("disk full")

    ctl = C.Control(ScivoConfig(root=tmp_path, command="x"), "p", "pid", "m")
    ctl._client, ctl.link, ctl.sid = Client(), C.Link("pub", "url"), "s"
    ctl._net = netlog.Tally(every=0.0, sink=broken)
    assert asyncio.run(ctl._call("list_responses", pub_id="pub")).ok


def test_without_the_mcps_netlog_nothing_is_raised(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_netlog(name, *a, **kw):
        if name == "co_scientist_local":
            raise ImportError(name)
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", no_netlog)
    netlog.record("mcp", host="firestore", kind="x", ms=1, rc=0)
