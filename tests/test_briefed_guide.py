"""The harness asks for the guide as a host that has already done the calls.

`project_guide(briefed=True)` returns what the session-start results mean
instead of six calls the charter then tells the model not to make. An MCP from
before that argument refuses it, and must still yield a guide.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from scivo_harness import preflight, prompt, session, toolsets
from scivo_harness.scivo_mcp import ToolOutcome

_BODY = "\n## Conventions\n" + "A rule about citations. " * 400
OLD = "# guide\n\nOn every session start:\n\n1. Call `mcp__scivo__whoami()` once\n" + _BODY
NEW = "# guide\n\nThe host ran the session-start protocol before this conversation.\n" + _BODY
# What mcp 2.3 actually hands back when a server refuses an argument:
REFUSAL = ("Error executing tool project_guide: 1 validation error for project_guideArguments\n"
           "briefed\n  Extra inputs are not permitted [type=extra_forbidden, input_value=True]")


class _Tools:
    tools: list = []


class _Client:
    def __init__(self, knows_briefed: bool, garbage: str = ""):
        self.knows, self.calls, self.garbage = knows_briefed, [], garbage
        self._session = self

    async def list_tools(self):
        return _Tools()

    async def call(self, name, **arguments):
        self.calls.append((name, arguments))
        if name != "project_guide":
            return ToolOutcome(True, items=[])
        if arguments.get("briefed") and not self.knows:
            return ToolOutcome(False, error=REFUSAL)
        if self.garbage:
            return ToolOutcome(True, items=[self.garbage])
        return ToolOutcome(True, items=[NEW if arguments.get("briefed") else OLD])


def _survey(monkeypatch, client):
    @asynccontextmanager
    async def fake_connect(_config):
        yield client

    async def fake_gather(_client, _config):
        return preflight.Briefing(identity={"project_id": "p"})

    monkeypatch.setattr(session, "connect", fake_connect)
    monkeypatch.setattr(session, "gather", fake_gather)
    return asyncio.run(session.survey(object(), with_guide=True))


def test_a_current_mcp_is_asked_once_and_gives_the_briefed_guide(monkeypatch):
    client = _Client(knows_briefed=True)
    briefing, _names, guide = _survey(monkeypatch, client)
    assert guide == NEW
    assert [a for n, a in client.calls if n == "project_guide"] == [{"briefed": True}]
    assert not briefing.errors


def test_an_older_mcp_is_asked_again_the_old_way_and_no_error_is_reported(monkeypatch):
    client = _Client(knows_briefed=False)
    briefing, _names, guide = _survey(monkeypatch, client)
    assert guide == OLD
    assert [a for n, a in client.calls if n == "project_guide"] == [{"briefed": True}, {}]
    assert not briefing.errors


def _narrow_plan():
    names = ["whoami", "list_papers", "list_servers", "add_video"]
    return toolsets.plan(names, profile="video", read_only=False)


def test_a_narrow_profile_is_told_about_the_protocol_only_when_the_guide_lists_it():
    plan = _narrow_plan()
    assert any(n in plan.dropped for n in prompt.SESSION_START_TOOLS), "the fixture must drop one"
    brief = preflight.Briefing(identity={"project_id": "p"})
    assert "prescribes a session-start protocol" in prompt.build(brief, plan, guide=OLD)
    assert "prescribes a session-start protocol" not in prompt.build(brief, plan, guide=NEW)


def test_a_stale_process_reaches_the_briefing():
    brief = preflight.Briefing(identity={"project_id": "p", "stale_process": True,
                                         "stale_hint": "session abc123, disk def456"})
    out = preflight.to_markdown(brief)
    assert "Stale process" in out and "abc123" in out and "not a pass" in out
    assert "Stale process" not in preflight.to_markdown(preflight.Briefing(identity={"project_id": "p"}))


def test_an_answer_that_is_not_a_guide_is_reported_and_not_used(monkeypatch):
    """The permissive reading was the one that got believed: 0.3 KB of error
    text went into the prompt as "the authoritative conventions"."""
    briefing, _names, guide = _survey(monkeypatch, _Client(True, garbage=REFUSAL))
    assert guide is None
    assert any("is not the guide" in e for e in briefing.errors)


# ── the client, against what the installed mcp library actually returns ──

class _Block:
    type = "text"
    def __init__(self, text): self.text = text


class _Result:
    def __init__(self, text, **flags):
        self.content = [_Block(text)]
        for k, v in flags.items(): setattr(self, k, v)


class _RawSession:
    def __init__(self, result): self.result = result
    async def call_tool(self, _name, _arguments): return self.result


def _call(result):
    from scivo_harness.scivo_mcp import ScivoClient
    client = object.__new__(ScivoClient)
    client._session = _RawSession(result)
    return asyncio.run(client.call("project_guide", briefed=True))


def test_a_failed_call_is_a_failure_under_either_spelling_of_the_flag():
    assert not _call(_Result(REFUSAL, is_error=True))        # mcp 2.x
    assert not _call(_Result(REFUSAL, isError=True))         # mcp 1.x
    assert not _call(_Result(REFUSAL))                       # no flag at all: the text says so
    assert _call(_Result("an ordinary answer", is_error=False))


def test_the_prompt_does_not_tell_the_model_to_skip_the_topics_too():
    plan = toolsets.plan(["whoami", "project_guide"], profile="full", read_only=False)
    brief = preflight.Briefing(identity={"project_id": "p"})
    with_topics = NEW + '\nRead `project_guide(topic="study")` before `write_study`.\n'
    assert "that IS a call to make" in prompt.build(brief, plan, guide=with_topics)
    assert "that IS a call to make" not in prompt.build(brief, plan, guide=OLD)
