"""`scivo run` exits with what happened, because a script reads nothing else.

It returned 0 whatever happened — an API error, a budget that ran out, every
write refused for want of anyone to approve it — so the caller carried on as
if the work had been done (review 2026-11-10).
"""
from __future__ import annotations

import asyncio
import types

import pytest

from scivo_harness import cli


class Text:
    def __init__(self, text):
        self.text = text


class Assistant:
    def __init__(self, text):
        self.content = [Text(text)]


class Result:
    def __init__(self, is_error=False, stop_reason=None, result=""):
        self.is_error, self.stop_reason, self.result = is_error, stop_reason, result


def _run(monkeypatch, messages, denied=(), raises=None):
    import claude_agent_sdk as sdk

    async def query(prompt, options):
        for message in messages:
            yield message
        if raises:
            raise raises

    for name, value in (("AssistantMessage", Assistant), ("TextBlock", Text),
                        ("ResultMessage", Result), ("query", query)):
        monkeypatch.setattr(sdk, name, value, raising=False)
    closed = []
    session = types.SimpleNamespace(
        options=object(),
        endpoint=types.SimpleNamespace(note=None, close=lambda: closed.append(True)),
        approver=types.SimpleNamespace(denied=list(denied)))
    code = asyncio.run(cli._run_once(session, "do the thing"))
    return code, closed


def test_a_finished_turn_exits_zero(monkeypatch, capsys):
    code, closed = _run(monkeypatch, [Assistant("done."), Result()])
    assert code == cli.RUN_OK == 0
    assert capsys.readouterr().out.strip() == "done."
    assert closed == [True]


def test_a_turn_that_ended_in_an_error_does_not(monkeypatch, capsys):
    code, _ = _run(monkeypatch, [Assistant("partial"),
                                 Result(is_error=True, stop_reason="error_max_budget_usd",
                                        result="budget reached")])
    assert code == cli.RUN_FAILED == 1
    err = capsys.readouterr().err
    assert "error_max_budget_usd" in err and "budget reached" in err


def test_refused_tool_calls_are_their_own_exit_status(monkeypatch, capsys):
    """Finished, and did not do the work: the reply says so in prose, which a
    script does not read."""
    code, _ = _run(monkeypatch, [Assistant("I could not write the section."), Result()],
                   denied=["mcp__scivo__update_section", "mcp__scivo__update_section", "Bash"])
    assert code == cli.RUN_DENIED == 3
    err = capsys.readouterr().err
    assert "3 tool call(s) were refused" in err
    assert "update_section" in err and "Bash" in err and "mcp__scivo__" not in err


def test_a_stream_that_stops_without_a_result_is_not_a_success(monkeypatch, capsys):
    code, _ = _run(monkeypatch, [Assistant("half a sent")])
    assert code == cli.RUN_FAILED
    assert "without reporting a result" in capsys.readouterr().err


def test_the_endpoint_is_closed_even_when_the_turn_raises(monkeypatch):
    """A local provider's shim is a thread and a port; a one-shot that raised
    used to leave it to the interpreter's exit."""
    import claude_agent_sdk as sdk

    async def query(prompt, options):
        raise RuntimeError("cli went away")
        yield  # pragma: no cover

    for name, value in (("AssistantMessage", Assistant), ("TextBlock", Text),
                        ("ResultMessage", Result), ("query", query)):
        monkeypatch.setattr(sdk, name, value, raising=False)
    closed = []
    session = types.SimpleNamespace(
        options=object(), approver=types.SimpleNamespace(denied=[]),
        endpoint=types.SimpleNamespace(note=None, close=lambda: closed.append(True)))
    with pytest.raises(RuntimeError):
        asyncio.run(cli._run_once(session, "x"))
    assert closed == [True]


def test_the_exit_statuses_are_in_the_help():
    parser = cli._parser()
    run = next(a for a in parser._subparsers._group_actions[0].choices.items() if a[0] == "run")[1]
    text = run.format_help()
    assert "0" in text and "1" in text and "3" in text and "refused" in text
