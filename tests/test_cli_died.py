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
