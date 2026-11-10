"""The CLI going away costs the process, not the session.

`CLIConnectionError: Cannot write to terminated process (exit code: 143)`
escaped `run()`, escaped `main`, and printed a traceback — the least useful
thing to show, since the transcript was safe on disk and the remedy was one
line (user, 2026-11-09, on a loaded machine).
"""
from __future__ import annotations

import asyncio
import types

import pytest

from scivo_harness.failures import SIGNALS, _why_it_stopped, explain
from scivo_harness.repl import Repl


class DeadClient:
    def __init__(self, connects: bool = True):
        self.connects = connects
        self.options = None
        self.disconnected = False

    async def connect(self):
        if not self.connects:
            raise RuntimeError("still gone")

    async def disconnect(self):
        self.disconnected = True

    async def set_permission_mode(self, mode):
        pass


def _repl(monkeypatch, session_id="sess-1", reconnects=True):
    options = types.SimpleNamespace(permission_mode="default", resume=None,
                                    continue_conversation=False)
    # A real dataclass is not needed: `replace` is imported inside the method,
    # so the stand-in only has to survive it — see the test that pins that.
    from dataclasses import dataclass, field

    @dataclass
    class Opts:
        permission_mode: str = "default"
        resume: str | None = None
        continue_conversation: bool = False

    session = types.SimpleNamespace(options=Opts(), provider=types.SimpleNamespace(name="anthropic"))
    stub = types.SimpleNamespace(session=session, session_id=session_id, mode="default",
                                 control=None, said=[])
    stub._say = stub.said.append
    stub._stop_pump = lambda: asyncio.sleep(0)
    stub._start_pump = lambda: asyncio.sleep(0)
    stub.client = DeadClient()
    stub._reconnect = Repl._reconnect.__get__(stub)
    import scivo_harness.repl as repl_mod
    # Through monkeypatch, so it is put back. Assigning the module attribute
    # directly leaked a DeadClient into every later test module that builds a
    # client — four failures in files that had nothing to do with this one,
    # and only in a full run.
    monkeypatch.setattr(repl_mod, "ClaudeSDKClient",
                        lambda options=None: DeadClient(connects=reconnects))
    return stub


def _text(stub) -> str:
    from scivo_harness.repl import ANSI
    return ANSI.sub("", "\n".join(stub.said))


def test_it_reconnects_on_the_same_conversation(monkeypatch):
    stub = _repl(monkeypatch)
    exc = RuntimeError("Cannot write to terminated process (exit code: 143)")
    assert asyncio.run(stub._reconnect(exc)) is True
    assert stub.session.options.resume == "sess-1", "the transcript is on disk"
    assert stub.session.options.continue_conversation is False
    said = _text(stub)
    assert "reconnected" in said
    # The turn that was running did not finish, and saying so is the
    # difference between a hiccup and a silently dropped question.
    assert "say it again" in said


def test_it_reads_the_exit_code_rather_than_printing_it(monkeypatch):
    stub = _repl(monkeypatch)
    asyncio.run(stub._reconnect(RuntimeError("terminated process (exit code: 143)")))
    said = _text(stub)
    assert "143" in said and "SIGTERM" in said
    assert "out-of-memory" in said or "kill" in said


def test_without_a_session_id_it_says_so_instead_of_starting_over(monkeypatch):
    """Reconnecting with no id lands in an empty conversation, which looks
    like the old one until you ask it something."""
    stub = _repl(monkeypatch, session_id=None)
    assert asyncio.run(stub._reconnect(RuntimeError("terminated (exit code: 143)"))) is False
    assert "no id to resume" in _text(stub)


def test_a_failed_reconnect_keeps_the_resume_line(monkeypatch):
    stub = _repl(monkeypatch, reconnects=False)
    assert asyncio.run(stub._reconnect(RuntimeError("terminated (exit code: 137)"))) is False
    said = _text(stub)
    assert "could not reconnect" in said
    assert "scivo resume sess-1"[:18] in said.replace("`", "")


@pytest.mark.parametrize("code,word", [
    (143, "SIGTERM"), (137, "SIGKILL"), (139, "SIGSEGV"), (129, "SIGHUP"), (130, "SIGINT"),
])
def test_every_signal_it_claims_to_know_is_explained(code, word):
    out = _why_it_stopped(f"exit code: {code}")
    assert word in out
    assert len(out) > len(word) + 10, "a name without a cause helps nobody"
    assert code in SIGNALS


def test_an_unknown_signal_is_named_as_a_signal_not_guessed():
    out = _why_it_stopped("exit code: 141")
    assert "signal 13" in out, out
    assert "kill -l 13" in out


def test_a_plain_exit_is_not_called_a_signal():
    out = _why_it_stopped("exit code: 2")
    assert "signal" not in out
    assert "exited on its own" in out


def test_no_exit_code_is_not_read_as_a_clean_stop():
    """"could not be said from here" — not a guess, and not silence."""
    out = _why_it_stopped("the process is gone")
    assert "cannot be said" in out


def test_the_explainer_covers_this_failure_too():
    said = explain(RuntimeError(
        "Cannot write to terminated process (exit code: 143)"))
    assert said and "143" in said and "SIGTERM" in said
    assert "scivo resume" in said


# ── the quieter way to lose the CLI: it is alive and we cannot hear it ──────

def _turn_stub(monkeypatch, pump, session_id="sess-1"):
    from scivo_harness.repl import Repl
    stub = _repl(monkeypatch, session_id=session_id)
    stub.pump = pump
    stub.queries = []

    class Client(DeadClient):
        async def query(self, line):
            stub.queries.append(line)

    stub.client = Client()
    stub.session.rails = types.SimpleNamespace(new_turn=lambda: None, looping=None)
    stub.attachments = []
    stub._turn = Repl._turn.__get__(stub)
    stub._guarded_turn = Repl._guarded_turn.__get__(stub)
    return stub


def test_a_turn_is_not_sent_to_a_cli_nobody_can_hear(monkeypatch):
    """A prompt sent to a CLI whose output we cannot read is a turn that runs
    — tools and all — with nobody watching. Refused BEFORE the query."""
    from scivo_harness.pump import StreamEnded

    pump = types.SimpleNamespace(ended=True, why_ended=lambda: "MessageParseError: bad frame")
    stub = _turn_stub(monkeypatch, pump)
    with pytest.raises(StreamEnded) as caught:
        asyncio.run(stub._turn(stub.client, "carry on"))
    assert "bad frame" in str(caught.value)
    assert stub.queries == []


def test_a_dead_stream_reconnects_instead_of_reporting_a_failed_turn(monkeypatch):
    """Reporting it as one failed turn and carrying on would fail every later
    turn the same way; the session needs a new reader."""
    pump = types.SimpleNamespace(ended=True, why_ended=lambda: "the stream closed")
    stub = _turn_stub(monkeypatch, pump)
    assert asyncio.run(stub._guarded_turn("carry on")) is True
    said = _text(stub)
    assert "stopped hearing from Claude Code" in said and "reconnected" in said
    # Not the wording for a killed process: nothing was killed.
    assert "process stopped" not in said
    assert stub.session.options.resume == "sess-1"


def test_a_dead_stream_with_no_session_to_resume_ends_the_session(monkeypatch):
    pump = types.SimpleNamespace(ended=True, why_ended=lambda: "the stream closed")
    stub = _turn_stub(monkeypatch, pump, session_id=None)
    assert asyncio.run(stub._guarded_turn("carry on")) is False
    assert "nothing to reconnect to" in _text(stub)


def test_a_stream_that_ends_mid_turn_reaches_the_reconnect(monkeypatch):
    """Through the real `_turn` loop with a real pump: the turn's own
    `except Exception` used to take this as an ordinary failed turn, print it
    and return — after which the session looked alive and was not."""
    from scivo_harness.pump import MessagePump
    from scivo_harness.repl import Repl

    async def main():
        feed: asyncio.Queue = asyncio.Queue()

        class Source:
            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await feed.get()
                if isinstance(item, Exception):
                    raise item
                return item

        pump = MessagePump(Source(), lambda m: None)
        await pump.start()
        stub = _turn_stub(monkeypatch, pump)
        stub.control = None
        stub._stopping = False
        stub._last_output = 0.0
        stub._typed_ahead = ""
        stub._heartbeat = lambda: asyncio.sleep(3600)
        stub._clear_activity = lambda: None
        stub._context = lambda client, brief: asyncio.sleep(0)

        async def query(line):
            stub.queries.append(line)
            feed.put_nowait(RuntimeError("JSON message exceeded maximum buffer size"))

        stub.client.query = query
        went_on = await stub._guarded_turn("show me the slide")
        return stub, went_on

    stub, went_on = asyncio.run(main())
    assert stub.queries == ["show me the slide"]
    assert went_on is True
    said = _text(stub)
    assert "stopped hearing from Claude Code" in said and "buffer size" in said
    assert "reconnected" in said
