"""Being told that a remote job finished, without having to ask.

The row was closed and the dashboard knew; the session that launched it was
never told. An agent blocked on `wait_for_run` for seven minutes and then told
the user a finished job was still running (feedback ecba6396d9b4).

The MCP stamps each run with the client that recorded it and `run_notices`
answers what closed since a watermark. This is the loop that asks — between
turns, holding nothing — and hands the answer to the same offer the finished
background tasks go through.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from scivo_harness.repl import RUN_CREATING_TOOLS, ANSI, Repl


def _repl(notify: str = "ask"):
    stub = types.SimpleNamespace(
        control=None, notify_mode=notify, _wake=asyncio.Event(), _wake_notes=[],
        _runs_task=None, _runs_since=None, said=[], tool_calls=0,
    )
    stub._say = stub.said.append
    for name in ("_on_run_closed", "_consume_wake", "_wake_line", "_watch_runs"):
        setattr(stub, name, getattr(Repl, name).__get__(stub))
    return stub


def _notice(**over):
    return {"slug": "p", "analysis": "asm", "run_key": "run_1", "host": "b200",
            "command": "hifiasm -o x", "exit_code": 0,
            "log_path": "analysis/asm/run.log", **over}


def test_a_finished_run_is_printed_and_queued(capsys):
    stub = _repl()
    stub._on_run_closed(_notice())
    printed = ANSI.sub("", capsys.readouterr().out)
    assert "run run_1 exit 0" in printed and "p/asm" in printed
    assert stub._wake.is_set()
    assert "run run_1 in p/asm finished, exit 0" in stub._wake_notes[0]
    assert "analysis/asm/run.log" in stub._wake_notes[0]


def test_a_run_that_nobody_recorded_a_status_for_says_so(capsys):
    stub = _repl()
    stub._on_run_closed(_notice(exit_code=-2, caution="read the log before reporting"))
    printed = ANSI.sub("", capsys.readouterr().out)
    assert "exit -2" in printed and "read the log" in printed
    # The note the model gets must carry it too — "finished" is not
    # "succeeded", and -2 is the case that reads as success by default.
    assert "nothing recorded whether it worked" in stub._wake_notes[0]


def test_notify_off_prints_without_queueing(capsys):
    stub = _repl(notify="off")
    stub._on_run_closed(_notice())
    assert "run run_1" in ANSI.sub("", capsys.readouterr().out)
    assert not stub._wake.is_set() and stub._wake_notes == []


def test_a_failed_run_is_queued_as_well(capsys):
    stub = _repl()
    stub._on_run_closed(_notice(exit_code=1))
    assert "exit 1" in ANSI.sub("", capsys.readouterr().out)
    assert stub._wake.is_set()


def test_the_tools_that_start_the_watch_are_the_ones_that_make_a_run():
    # Bare names: `scivo_tool_label` has already stripped the prefix by the
    # time the dispatch compares them.
    assert RUN_CREATING_TOOLS == {"submit_remote_job", "launch_local_job",
                                  "record_analysis_run"}
    assert not any(name.startswith("mcp__") for name in RUN_CREATING_TOOLS)


def test_the_watch_is_not_started_twice():
    started = []

    async def drive():
        stub = _repl()
        stub._follow_runs = lambda: _sleep_forever(started)
        stub._watch_runs()
        stub._watch_runs()
        await asyncio.sleep(0)
        task = stub._runs_task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _sleep_forever(log):
        log.append(1)
        await asyncio.Event().wait()

    asyncio.run(drive())
    assert started == [1], "a second submitted run must not start a second loop"


# ── the loop that asks ────────────────────────────────────────────────────

class _FakeClient:
    """`run_notices` answers in sequence; the last answer repeats."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    async def call(self, name, **arguments):
        self.calls.append({"name": name, **arguments})
        payload = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return types.SimpleNamespace(ok=True, items=[payload], first=payload,
                                     error=None)


def _bind_watch(stub):
    """The real loop and what it reads, on a stand-in for the REPL."""
    for name, value in (("control", None), ("_run_homes", set()),
                        ("_run_homes_complete", True), ("_runs_scoped", True)):
        if not hasattr(stub, name):
            setattr(stub, name, value)
    for name in ("_follow_runs", "_poll_runs", "_note_run_home"):
        setattr(stub, name, getattr(Repl, name).__get__(stub))


def _run_loop(stub, client, monkeypatch):
    import contextlib

    from scivo_harness import repl as repl_mod
    from scivo_harness import scivo_mcp

    monkeypatch.setattr(repl_mod, "RUN_POLL_SECONDS", 0.0)

    @contextlib.asynccontextmanager
    async def fake_connect(_config):
        yield client

    monkeypatch.setattr(scivo_mcp, "connect", fake_connect)
    stub.session = types.SimpleNamespace(config=object())
    _bind_watch(stub)
    asyncio.run(asyncio.wait_for(stub._follow_runs(), timeout=5))


def test_the_loop_delivers_a_notice_then_stops_when_nothing_is_open(monkeypatch, capsys):
    stub = _repl()
    client = _FakeClient([
        {"now": "T1", "closed": [_notice()], "still_open": 1},
        {"now": "T2", "closed": [], "still_open": 0},
    ])
    _run_loop(stub, client, monkeypatch)
    assert len(client.calls) == 2, "it must stop once nothing of ours is open"
    assert "run run_1 in p/asm finished" in stub._wake_notes[0]
    assert len(stub._wake_notes) == 1, "announced once"
    # The watermark went back on the next call, so a close is not re-announced.
    assert client.calls[1]["since"] == "T1"
    assert stub._runs_since == "T2"


def test_the_first_call_passes_no_watermark(monkeypatch):
    stub = _repl()
    client = _FakeClient([{"now": "T1", "closed": [], "still_open": 0}])
    _run_loop(stub, client, monkeypatch)
    assert "since" not in client.calls[0]


def test_an_mcp_that_does_not_know_the_tool_stops_being_asked(monkeypatch):
    from scivo_harness.repl import RUN_POLL_GIVE_UP

    stub = _repl()

    class Refuses:
        def __init__(self):
            self.calls: list = []

        async def call(self, name, **arguments):
            self.calls.append(name)
            return types.SimpleNamespace(ok=False, items=[], first=None,
                                         error="Unknown tool: run_notices")

    client = Refuses()
    _run_loop(stub, client, monkeypatch)       # must not raise
    assert len(client.calls) == RUN_POLL_GIVE_UP
    assert stub._wake_notes == []


def test_one_lost_poll_does_not_cost_the_notification(monkeypatch):
    """A blip used to end the watch, and an eleven-minute job would then
    finish unannounced. Found by falsifying this file: deleting the refusal
    branch altogether broke no test, because the loop stopped on the empty
    answer anyway — so the branch had to earn its place."""
    stub = _repl()

    class Flaky:
        def __init__(self):
            self.n = 0

        async def call(self, name, **arguments):
            self.n += 1
            if self.n <= 2:
                return types.SimpleNamespace(ok=False, items=[], first=None,
                                             error="pipe closed")
            payload = {"now": "T9", "closed": [_notice()], "still_open": 0}
            return types.SimpleNamespace(ok=True, items=[payload], first=payload,
                                         error=None)

    _run_loop(stub, Flaky(), monkeypatch)
    assert len(stub._wake_notes) == 1, "the notice was lost to a transient failure"


def test_a_transport_error_does_not_end_the_session(monkeypatch):
    stub = _repl()

    class Explodes:
        async def call(self, name, **arguments):
            raise RuntimeError("the pipe closed")

    _run_loop(stub, Explodes(), monkeypatch)   # must not raise
    assert stub._wake_notes == []


def test_each_poll_is_written_to_the_shared_outbound_log(monkeypatch, tmp_path):
    """The three things under suspicion for the wifi drops are an SSH
    keepalive, an SSH `ps` poll and this Firestore poll (todo d27e0606a7bb).
    They have to land in ONE file or they cannot be compared against the
    moment the link went down."""
    netlog = pytest.importorskip(
        "co_scientist_local.netlog",
        reason="the MCP is a dependency in production; a bare dev venv may lack it")

    target = tmp_path / "net.log"
    monkeypatch.setenv("SCIVO_NETLOG_PATH", str(target))
    monkeypatch.delenv("SCIVO_NETLOG", raising=False)
    stub = _repl()
    client = _FakeClient([{"now": "T1", "closed": [], "still_open": 0}])
    _run_loop(stub, client, monkeypatch)
    rows = [l.split("\t") for l in target.read_text(encoding="utf-8").splitlines()]
    assert rows and rows[0][1:4] == ["mcp", "firestore", "run_notices"]
    assert rows[0][5] == "0"
    assert netlog.classify("whatever") == "other"   # the module is the real one


def test_a_refused_poll_is_logged_as_refused(monkeypatch, tmp_path):
    pytest.importorskip("co_scientist_local.netlog")
    target = tmp_path / "net.log"
    monkeypatch.setenv("SCIVO_NETLOG_PATH", str(target))
    monkeypatch.delenv("SCIVO_NETLOG", raising=False)
    stub = _repl()

    class Refuses:
        async def call(self, name, **arguments):
            return types.SimpleNamespace(ok=False, items=[], first=None, error="nope")

    _run_loop(stub, Refuses(), monkeypatch)
    rows = [l.split("\t") for l in target.read_text(encoding="utf-8").splitlines()]
    assert rows and all(r[5] == "1" for r in rows)


def test_one_client_id_for_every_mcp_this_session_starts(monkeypatch):
    """The session starts two MCP processes — the one the model calls tools
    through, and the one this watcher opens — and the MCP minted its id per
    PROCESS. So `run_notices`, filtered by the caller's id, saw none of the
    session's runs, found nothing open and stopped on the first poll: silent,
    by design, while a finished job waited 92 minutes (feedback f7c878a2b9c6).
    """
    import os

    from scivo_harness.session import session_client_id

    monkeypatch.delenv("SCIVO_CLIENT_ID", raising=False)
    first = session_client_id()
    assert first and first == os.environ["SCIVO_CLIENT_ID"], (
        "it must be in OUR environment too — config.child_env() is what the "
        "watcher's own connection inherits")
    # Asked twice in one session, it is the same client both times.
    assert session_client_id() == first


def test_a_pinned_client_id_is_kept(monkeypatch):
    from scivo_harness.session import session_client_id

    monkeypatch.setenv("SCIVO_CLIENT_ID", "pinned-by-the-host")
    assert session_client_id() == "pinned-by-the-host"


def test_the_cli_is_told_the_client_id_too():
    """Both MCP processes have to read the same one. The watcher's connection
    inherits it through `config.child_env()`; the model's MCP is started by the
    CLI, so the CLI has to carry it. Checked in the source, because that dict
    is not reachable without a live session — the same way the discovery-cache
    rule beside it is checked."""
    import inspect

    from scivo_harness import session

    assert session.session_env()["SCIVO_CLIENT_ID"] == session.session_client_id()
    assert "**session_env()" in inspect.getsource(session.build)


# ── where the runs are, and over which connection ───────────────────────────
# `run_notices` read every run of every analysis in the project to find this
# session's: 800 documents a call in one real project, every thirty seconds.
# And each watch started an MCP server process of its own — the third per
# session, ~120 MB.

_DONE = {"closed": [], "still_open": 0, "now": "t1"}


def test_the_watch_says_where_it_filed_its_runs(monkeypatch):
    stub = _repl()
    _bind_watch(stub)
    stub._note_run_home({"slug": "p", "analysis": "asm", "command": "hifiasm"})
    stub._note_run_home({"slug": "_project", "analysis": "render"})
    client = _FakeClient([_DONE])
    _run_loop(stub, client, monkeypatch)
    assert client.calls[0]["analyses"] == [["_project", "render"], ["p", "asm"]]


def test_a_run_filed_without_saying_where_turns_the_scope_off(monkeypatch):
    """A watch that looked in the wrong place would report a job still running
    after it had finished — the failure the watch exists to end."""
    stub = _repl()
    _bind_watch(stub)
    stub._note_run_home({"slug": "p", "analysis": "asm"})
    stub._note_run_home({"command": "something"})          # no slug, no analysis
    client = _FakeClient([_DONE])
    _run_loop(stub, client, monkeypatch)
    assert "analyses" not in client.calls[0]


def test_an_mcp_that_refuses_the_argument_is_asked_the_old_way(monkeypatch):
    class Older(_FakeClient):
        async def call(self, name, **arguments):
            self.calls.append({"name": name, **arguments})
            if "analyses" in arguments:
                return types.SimpleNamespace(
                    ok=False, items=[], first=None,
                    error="Error executing tool run_notices: analyses: Extra inputs are not permitted")
            payload = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            return types.SimpleNamespace(ok=True, items=[payload], first=payload, error=None)

    stub = _repl()
    _bind_watch(stub)
    stub._note_run_home({"slug": "p", "analysis": "asm"})
    client = Older([{"closed": [], "still_open": 1, "now": "t1"}, _DONE])
    _run_loop(stub, client, monkeypatch)
    asked = [("analyses" in call) for call in client.calls]
    assert asked == [True, False, False], asked            # tried once, never again
    assert stub._runs_scoped is False


def test_the_pages_connection_is_used_when_there_is_a_page(monkeypatch):
    from scivo_harness import repl as repl_mod
    from scivo_harness import scivo_mcp

    monkeypatch.setattr(repl_mod, "RUN_POLL_SECONDS", 0.0)
    opened = []

    def no_connect(_config):
        opened.append(1)
        raise AssertionError("a second MCP server was started")
    monkeypatch.setattr(scivo_mcp, "connect", no_connect)
    shared = _FakeClient([_DONE])
    stub = _repl()
    stub.control = types.SimpleNamespace(active=True, _client=shared)
    stub.session = types.SimpleNamespace(config=object())
    _bind_watch(stub)
    asyncio.run(asyncio.wait_for(stub._follow_runs(), timeout=5))
    assert opened == [] and len(shared.calls) == 1


def test_a_page_connection_that_stops_answering_hands_over_to_its_own(monkeypatch):
    class Dead(_FakeClient):
        async def call(self, name, **arguments):
            self.calls.append({"name": name, **arguments})
            return types.SimpleNamespace(ok=False, items=[], first=None,
                                         error="ClosedResourceError: ")

    dead, own = Dead([{}]), _FakeClient([_DONE])
    stub = _repl()
    stub.control = types.SimpleNamespace(active=True, _client=dead)
    _run_loop(stub, own, monkeypatch)
    assert len(dead.calls) == 3, "three refusals, as before, then it moves"
    assert len(own.calls) == 1


def test_an_mcp_with_no_run_notices_is_not_asked_over_a_second_connection(monkeypatch):
    """A tool that is not there is not there on any connection."""
    from scivo_harness import repl as repl_mod
    from scivo_harness import scivo_mcp

    class NoTool(_FakeClient):
        async def call(self, name, **arguments):
            self.calls.append({"name": name})
            return types.SimpleNamespace(ok=False, items=[], first=None,
                                         error="Unknown tool: run_notices")

    monkeypatch.setattr(repl_mod, "RUN_POLL_SECONDS", 0.0)
    monkeypatch.setattr(scivo_mcp, "connect",
                        lambda _c: (_ for _ in ()).throw(AssertionError("opened another")))
    shared = NoTool([{}])
    stub = _repl()
    stub.control = types.SimpleNamespace(active=True, _client=shared)
    stub.session = types.SimpleNamespace(config=object())
    _bind_watch(stub)
    asyncio.run(asyncio.wait_for(stub._follow_runs(), timeout=5))
    assert len(shared.calls) == 3
