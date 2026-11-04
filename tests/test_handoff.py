"""The handoff written when a conversation is about to be compacted.

A compaction is the one moment where detail is discarded for good, and nothing
durable recorded what the session had learned (user, 2026-11-04). Claude Code's
own summary is not that record: it is written for the next turn's continuity,
it lives only in this machine's transcript, and it is reachable only as an
untyped `user` message beginning "This session is being continued…" — a private
format a string match would depend on.

So the handoff is asked for rather than scraped, through the one compaction
event that exists (`PreCompact`, which fires before the summary is written),
and a person can ask for it too. Both send the same words, from one definition.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from scivo_harness.guardrails import Guardrails, build, handoff_enabled
from scivo_harness.prompt import HANDOFF_ASK
from scivo_harness.repl import LOCAL_COMMANDS


def _hook(rails=None, **env):
    for key, value in env.items():
        os.environ[key] = value
    try:
        return asyncio.run((rails or Guardrails()).on_compact({}, None, None))
    finally:
        for key in env:
            os.environ.pop(key, None)


def test_the_hook_asks_for_the_handoff_before_a_compaction():
    out = _hook()
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreCompact"
    assert specific["additionalContext"] == HANDOFF_ASK


def test_it_asks_for_identifiers_and_not_a_recap():
    # What makes the entry worth reading later: things that find the work
    # again. A second narrative summary is what the transcript already has.
    assert "log_activity" in HANDOFF_ASK
    for wanted in ("open", "id", "read first", "shipped"):
        assert wanted in HANDOFF_ASK, wanted
    assert "No narrative" in HANDOFF_ASK


def test_off_asks_for_nothing():
    assert _hook(SCIVO_HANDOFF="off") == {}
    assert _hook(SCIVO_HANDOFF="0") == {}


def test_on_by_default_because_nobody_is_watching_a_compaction():
    os.environ.pop("SCIVO_HANDOFF", None)
    assert handoff_enabled() is True
    os.environ["SCIVO_HANDOFF"] = "typo"
    try:
        assert handoff_enabled() is True, "a typo must not silence it"
    finally:
        os.environ.pop("SCIVO_HANDOFF", None)


def test_the_hook_is_registered_for_the_only_compaction_event_there_is():
    rails = Guardrails()
    events = build(rails)
    assert "PreCompact" in events
    assert len(events["PreCompact"]) == 1
    # There is no PostCompact in the SDK; asking before the summary exists is
    # the only option, and the better one — we want what the session knows.
    from claude_agent_sdk.types import HookEvent
    import typing
    assert "PostCompact" not in [
        arg for literal in typing.get_args(HookEvent) for arg in typing.get_args(literal)]


def test_a_person_can_ask_for_it_too():
    assert "/handoff" in LOCAL_COMMANDS
    assert "automatic" in LOCAL_COMMANDS["/handoff"]


def test_both_paths_send_one_text():
    """Two copies of an instruction drift, and the drift is invisible: one
    path would quietly ask for something else."""
    import scivo_harness.repl as repl_mod
    import scivo_harness.guardrails as rails_mod

    assert rails_mod.HANDOFF_ASK is HANDOFF_ASK
    assert repl_mod.HANDOFF_ASK is HANDOFF_ASK
    # And the dispatch actually sends it. A static read, because the REPL's
    # command loop is not exercised here — found by falsifying this file:
    # importing the constant and then sending different words passed
    # everything.
    import pathlib
    source = (pathlib.Path(repl_mod.__file__)).read_text(encoding="utf-8")
    assert "await self._turn(self.client, HANDOFF_ASK)" in source
