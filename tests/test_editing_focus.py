"""Where the session is editing, published for the dashboard to draw.

The one affordance another editor had that ours did not: you could see where
the agent was working (user, 2026-11-09). The harness sees every tool call
with its arguments, so the paragraph is known before the write lands.
"""
from __future__ import annotations

import asyncio
import types

from scivo_harness.repl import EDITING_TOOLS, RUN_CREATING_TOOLS
from scivo_harness.scivo_mcp import ToolOutcome


class FakeControl:
    active = True

    def __init__(self):
        self.focuses: list[dict] = []

    def focus(self, **kwargs):
        self.focuses.append(kwargs)


def test_the_editing_tools_are_named_not_guessed():
    """`add_figure` takes a slug too and edits nothing a reader is looking at;
    a marker on the wrong paragraph is worse than none."""
    assert "update_section" in EDITING_TOOLS
    assert "replace_in_section" in EDITING_TOOLS
    assert "add_figure" not in EDITING_TOOLS
    assert "submit_remote_job" not in EDITING_TOOLS
    # And the two sets are about different things.
    assert not (EDITING_TOOLS & RUN_CREATING_TOOLS)


def _control():
    """A Control with the focus machinery, nothing wired to a network."""
    from scivo_harness.control import Control

    c = Control.__new__(Control)
    c._client = types.SimpleNamespace(call=None)
    c.events = []
    c.errors = []
    c._dirty = set()
    c._lock = asyncio.Lock()
    c._last_flush = None
    c._stuck_said = False
    c._focus = None
    c._focus_dirty = False
    c._wake = asyncio.Event()
    c.sid = "s1"
    c.link = types.SimpleNamespace(pub_id="pub1")
    return c


def test_focus_records_the_paragraph_and_wakes_the_writer():
    c = _control()
    c.focus(slug="my-paper", section="methods", block_id="a1b2", tool="update_section")
    assert c._focus_dirty is True
    assert c._wake.is_set(), "a caret is only useful while the edit is happening"
    assert c._focus["slug"] == "my-paper"
    assert c._focus["section"] == "methods"
    assert c._focus["block_id"] == "a1b2"
    assert c._focus["doc"] == "focus" and c._focus["sid"] == "s1"


def test_focus_carries_a_timestamp_so_it_can_go_stale_by_itself():
    """A session that dies mid-edit never sends a clear, so the reader decides
    when the marker is old rather than waiting for one."""
    c = _control()
    c.focus(slug="p", section="s", block_id=None, tool="update_section")
    assert isinstance(c._focus["at"], int) and c._focus["at"] > 0


def test_a_focus_with_no_paper_is_not_published():
    c = _control()
    c.focus(slug="", section="s", block_id="x", tool="update_section")
    assert c._focus is None and c._focus_dirty is False


def test_the_flush_writes_the_focus_before_the_chunks():
    """The marker is only useful while the edit is happening; behind a burst of
    deltas it would arrive after the paragraph had already changed."""
    written: list[str] = []

    async def record(name, **kwargs):
        written.append(kwargs.get("doc_id") or "")
        return ToolOutcome(ok=True)

    c = _control()
    c._client = types.SimpleNamespace(call=record)
    c.events = [{"i": 0}]
    c._dirty = {0}
    c.focus(slug="p", section="s", block_id="b", tool="update_section")
    asyncio.run(c._flush())
    assert written[0] == "focus", written
    assert any(d.startswith("log-") for d in written[1:]), written


def test_a_focus_alone_is_enough_to_flush():
    """Nothing in the log changed, but the marker still has to go out."""
    written: list[str] = []

    async def record(name, **kwargs):
        written.append(kwargs.get("doc_id") or "")
        return ToolOutcome(ok=True)

    c = _control()
    c._client = types.SimpleNamespace(call=record)
    c.focus(slug="p", section="s", block_id="b", tool="update_section")
    assert asyncio.run(c._flush()) is True
    assert written == ["focus"]


def test_a_refused_focus_write_is_recorded_not_swallowed():
    async def refuse(name, **kwargs):
        return ToolOutcome(ok=False, error="gone")

    c = _control()
    c._client = types.SimpleNamespace(call=refuse)
    c.focus(slug="p", section="s", block_id="b", tool="update_section")
    asyncio.run(c._flush())
    assert c.errors and "focus" in c.errors[-1]


def test_it_is_not_retried_forever():
    """Unlike a chunk, a stale caret is worth nothing: the next edit replaces
    it, so a failed write is dropped rather than queued."""
    async def refuse(name, **kwargs):
        return ToolOutcome(ok=False, error="gone")

    c = _control()
    c._client = types.SimpleNamespace(call=refuse)
    c.focus(slug="p", section="s", block_id="b", tool="update_section")
    asyncio.run(c._flush())
    assert c._focus_dirty is False
