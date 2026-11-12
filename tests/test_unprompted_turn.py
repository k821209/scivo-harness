"""A turn the CLI starts by itself is shown, and is not asked for twice.

When a background task finishes, the CLI runs a turn on the notification at
once and the model reports there. The harness read those messages between its
own turns and dropped them — "nothing to show" — so the report reached
neither the terminal nor the page; then its wake-up asked the model to pick
the task up, and the model said it already had. A day of results in a session
built on long remote jobs went to nobody (feedback 062109d59514), and so did
this harness's own configuration review, read out of the transcript later.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from scivo_harness import repl as R
from scivo_harness.pump import MessagePump
from scivo_harness.repl import Repl

REPORT = "bcalm 상한별 메모리 표: 8 GB → 31 GB"


class Page:
    """What the web page is sent."""
    active = True

    def __init__(self):
        self.sent: list[tuple] = []
        self.waiting = False

    def delta(self, text): self.sent.append(("delta", text))
    def end_assistant(self): self.sent.append(("end",))
    def status(self, text, level="info"): self.sent.append(("status", text))
    def tool(self, *a): self.sent.append(("tool", *a))
    def focus(self, **kw): pass


def _repl(page=None):
    stub = types.SimpleNamespace(
        control=page, notify_mode="ask",
        session=types.SimpleNamespace(rails=types.SimpleNamespace(new_turn=lambda: None)),
        _wake=asyncio.Event(), _wake_notes=[], _wake_held=[], _wake_cli=set(), _unprompted=False,
        _streamed="", tool_calls=0, _tool_started={}, _activity_shown=False, _last_output=0.0,
        results=[],
    )
    for name in ("_on_idle_message", "_on_unprompted", "_on_stream", "_on_assistant", "_on_tool_result",
                 "_mark_output", "_clear_activity", "_hold_cli_wake", "_release_held_wakes",
                 "_withdraw_cli_wakes", "_consume_wake"):
        setattr(stub, name, getattr(Repl, name).__get__(stub))
    stub._on_result = stub.results.append            # its own tests are elsewhere
    stub._on_task_note = lambda m: None
    stub._on_task_update = lambda m: None
    stub._on_system = lambda m: None
    return stub


def _make(cls, **fields):
    """An instance of the class the harness itself tests with `isinstance`.

    Taken from the repl module, never imported here: under this suite's
    conftest the SDK is a stub that hands out a NEW class on every attribute
    access, so a class imported in a test is not the one the harness holds.
    Built without `__init__` so the same helper serves the real dataclasses.
    """
    obj = object.__new__(cls)
    for key, value in fields.items():
        object.__setattr__(obj, key, value)
    return obj


def _text(text):
    return _make(R.TextBlock, text=text)


def _assistant(*blocks):
    return _make(R.AssistantMessage, content=list(blocks), model="m")


def _delta(text):
    return _make(R.StreamEvent, uuid="u", session_id="s", parent_tool_use_id=None,
                 event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})


def _result():
    return _make(R.ResultMessage, subtype="success", is_error=False, num_turns=1, session_id="s")


def _answer(stub):
    for message in (_delta(REPORT[:10]), _delta(REPORT[10:]), _assistant(_text(REPORT)), _result()):
        stub._on_idle_message(message)


def test_a_report_written_between_turns_reaches_the_terminal_and_the_page(capsys):
    page = Page()
    stub = _repl(page)
    _answer(stub)
    out = capsys.readouterr().out
    assert REPORT in out                                       # the terminal
    assert "nobody typed this" in out                          # and it says whose turn it was
    assert "".join(t for kind, *rest in page.sent if kind == "delta" for t in rest) == REPORT
    assert ("end",) in page.sent                               # the page's message is closed
    assert page.waiting is False and stub._unprompted is False  # and the turn is over
    assert len(stub.results) == 1


def test_it_is_printed_once_not_once_as_deltas_and_again_whole(capsys):
    stub = _repl()
    _answer(stub)
    assert capsys.readouterr().out.count(REPORT) == 1


def test_a_tool_call_in_such_a_turn_is_shown_too(capsys):
    page = Page()
    stub = _repl(page)
    stub._on_idle_message(_assistant(
        _make(R.ToolUseBlock, id="t1", name="Bash", input={"command": "tail -3 run.log"})))
    assert "Bash" in capsys.readouterr().out
    assert any(kind == "tool" for kind, *_ in page.sent)
    assert stub._unprompted is True                            # still running until its result


@pytest.mark.parametrize("stray", [
    lambda: _make(R.UserMessage, content="<task-notification>…</task-notification>"),
    _result,
])
def test_the_notifications_own_echo_or_a_stray_result_starts_nothing(stray, capsys):
    """Only the model speaking is a turn. Otherwise an older CLI, which does
    not answer by itself, would have its wake-up withdrawn for nothing."""
    stub = _repl()
    stub._wake_notes, stub._wake_cli = ["task b51 completed"], {"task b51 completed"}
    stub._wake.set()
    stub._on_idle_message(stray())
    assert capsys.readouterr().out == ""
    assert stub._unprompted is False and stub._wake.is_set()


# ── the wake-up: offered only if the CLI does not answer ───────────────────

def test_a_task_the_cli_answers_is_not_offered_again(monkeypatch):
    monkeypatch.setattr(R, "WAKE_GRACE", 0.05)

    async def scenario():
        stub = _repl()
        stub._hold_cli_wake("task b51 completed")
        assert not stub._wake.is_set()                 # held, not offered at once
        _answer(stub)                                  # the CLI's own turn
        await asyncio.sleep(0.12)
        return stub
    stub = asyncio.run(scenario())
    assert not stub._wake.is_set() and stub._wake_notes == [] and stub._wake_held == []


def test_a_task_nobody_answers_is_offered_after_the_grace(monkeypatch):
    """An older CLI only queues the notification; then the wake-up is the
    only thing that will ever act on it."""
    monkeypatch.setattr(R, "WAKE_GRACE", 0.05)

    async def scenario():
        stub = _repl()
        stub._hold_cli_wake("task b51 completed")
        await asyncio.sleep(0.12)
        return stub
    stub = asyncio.run(scenario())
    assert stub._wake.is_set() and stub._wake_notes == ["task b51 completed"]


def test_a_run_notice_of_ours_is_not_withdrawn_by_the_cli_answering(monkeypatch):
    """The CLI knows its own tasks. A remote run closing is found by OUR
    watch, and nothing in the CLI's turn is about it."""
    monkeypatch.setattr(R, "WAKE_GRACE", 0.05)

    async def scenario():
        stub = _repl()
        stub._wake_notes.append("run run_1 in p/asm finished, exit 0")
        stub._wake.set()
        stub._hold_cli_wake("task b51 completed")
        _answer(stub)
        await asyncio.sleep(0.12)
        return stub
    stub = asyncio.run(scenario())
    assert stub._wake.is_set() and stub._wake_notes == ["run run_1 in p/asm finished, exit 0"]


def test_typing_a_turn_drops_what_was_held(monkeypatch):
    """The notification is already in the conversation the typed line joins."""
    monkeypatch.setattr(R, "WAKE_GRACE", 0.05)

    async def scenario():
        stub = _repl()
        stub._hold_cli_wake("task b51 completed")
        stub._consume_wake()
        await asyncio.sleep(0.12)
        return stub
    stub = asyncio.run(scenario())
    assert not stub._wake.is_set() and stub._wake_notes == []


def test_with_no_event_loop_a_wake_is_armed_at_once_as_before():
    stub = _repl()
    stub._hold_cli_wake("task b51 completed")
    assert stub._wake.is_set() and stub._wake_notes == ["task b51 completed"]


# ── through the real reader ────────────────────────────────────────────────

def test_the_reader_hands_an_unprompted_turn_over_in_order(capsys):
    async def scenario():
        stub = _repl(Page())

        async def source():
            for message in (_delta("first "), _delta("second"),
                            _assistant(_text("first second")), _result()):
                yield message
        pump = MessagePump(source(), stub._on_idle_message)
        await pump.start()
        await asyncio.sleep(0.05)
        await pump.stop()
        return stub, pump
    stub, pump = asyncio.run(scenario())
    assert "first second" in capsys.readouterr().out
    assert pump.idle_count == 4 and len(stub.results) == 1
