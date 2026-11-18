"""A session that stops leaves a record, and the next one reports it.

Feedback 515b505746ee (2026-11-18): a session stopped answering overnight,
was started again, and — asked what had happened — read its transcript and
found nothing wrong, because a harness whose loop has stopped writes nothing.
The restart had cleared the page's log too. These tests hold the record that
was missing: until when the loop ran, where it stood when it stopped, and the
sentence the next session says about it.
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from scivo_harness import pulse

SRC = str(Path(__file__).resolve().parent.parent / "src")
FAST = dict(tick=0.02, save_every=0.05, stall_after=0.3, watch_every=0.05)


def _read(p: pulse.Pulse) -> dict:
    return json.loads(p.path.read_text(encoding="utf-8"))


def _stuck_for(seconds: float) -> None:
    time.sleep(seconds)          # the loop's thread, not a task: nothing else can run


def test_a_running_session_stamps_its_file_and_a_clean_end_removes_it(tmp_path):
    async def go():
        p = pulse.Pulse(tmp_path, **FAST)
        p.start()
        first = _read(p)
        assert first["pid"] == os.getpid() and first["host"] == socket.gethostname()
        assert first["stall"] is None and first["stalls"] == 0
        await asyncio.sleep(0.4)
        assert pulse.left_behind(tmp_path, consume=False) == [], "this very process is not 'before'"
        await p.stop()
        return p
    p = asyncio.run(go())
    assert not p.path.exists()
    assert pulse.left_behind(tmp_path) == []


def test_a_loop_that_stops_is_recorded_with_where_it_stood_and_when_it_came_back(tmp_path):
    async def go():
        p = pulse.Pulse(tmp_path, **FAST)
        p.start()
        await asyncio.sleep(0.1)
        before = time.time()
        _stuck_for(0.9)
        during = _read(p)                       # written by the watching thread, not the loop
        await asyncio.sleep(0.3)
        after = _read(p)
        await p.stop()
        return before, during, after
    before, during, after = asyncio.run(go())
    stall = during["stall"]
    assert stall and stall["resumed"] is None and during["stalls"] == 1
    assert abs(pulse._parse(stall["since"]) - before) <= 2
    assert any("_stuck_for" in row and "time.sleep(seconds)" in row for row in stall["where"])
    assert any("test_pulse.py" in row for row in stall["where"])
    assert after["stall"]["resumed"], "and it is said when the loop turned again"
    assert after["stalls"] == 1


def test_the_stop_is_dated_from_when_the_loop_last_turned_not_from_when_it_was_noticed(tmp_path):
    clock = {"wall": pulse._parse("2026-11-17T22:10:00Z"), "mono": 5000.0}
    p = pulse.Pulse(tmp_path, now=lambda: clock["wall"], monotonic=lambda: clock["mono"])
    p._state = {"pid": os.getpid(), "stalls": 0, "stall": None}
    p._ticked = clock["mono"] - 200            # the loop last turned 200 s ago
    p._look()
    stall = _read(p)["stall"]
    assert stall["since"] == "2026-11-17T22:06:40Z" and stall["noticed"] == "2026-11-17T22:10:00Z"
    p._look()
    assert _read(p)["stalls"] == 1, "one stop, however many looks"


def test_a_busy_loop_is_not_a_stopped_one(tmp_path):
    async def go():
        p = pulse.Pulse(tmp_path, **FAST)
        p.start()
        for _ in range(8):
            _stuck_for(0.1)                     # under stall_after each time
            await asyncio.sleep(0.03)
        state = _read(p)
        await p.stop()
        return state
    assert asyncio.run(go())["stall"] is None


CHILD = textwrap.dedent("""
    import asyncio, sys
    sys.path.insert(0, {src!r})
    from pathlib import Path
    from scivo_harness import pulse

    async def main():
        p = pulse.Pulse(Path({root!r}), tick=0.02, save_every=0.05, stall_after=0.4, watch_every=0.05)
        p.start()
        await asyncio.sleep(0.2)
        {body}
        await p.stop()

    asyncio.run(main())
""")


def _child(tmp_path, body: str, **popen) -> subprocess.Popen:
    # A file, not `-c`: the record quotes the source line it stopped on.
    script = tmp_path / "session_standin.py"
    script.write_text(CHILD.format(src=SRC, root=str(tmp_path), body=body), encoding="utf-8")
    return subprocess.Popen([sys.executable, str(script)], **popen)


def _wait_for(check, seconds: float = 10.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        got = check()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("did not happen in time")


def test_a_session_blocked_writing_to_a_terminal_nobody_reads_says_so(tmp_path):
    """The mechanism, for real: a pipe that is never read fills, `print` does
    not return, and the loop — heartbeat, inbox and all — stops with it."""
    child = _child(tmp_path, "print('x' * 4_000_000, flush=True)", stdout=subprocess.PIPE)
    try:
        def stalled():
            files = list((tmp_path / pulse.DIR).glob("*.json"))
            state = json.loads(files[0].read_text()) if files else {}
            return state if state.get("stall") else None
        state = _wait_for(stalled)
        assert any("print(" in row for row in state["stall"]["where"][-2:])
        # Found by the next session while the stopped one is still there.
        (found,) = pulse.left_behind(tmp_path)
        assert found.pid == child.pid and found.running
        said = pulse.describe([found])
        assert "stopped responding at" in said and "STILL RUNNING" in said
        assert "write to its terminal" in said and f"`kill {child.pid}`" in said
        assert (tmp_path / pulse.DIR / f"{child.pid}.json").exists(), "reported again while it stands"
    finally:
        child.kill()
        child.wait()
        child.stdout.close()
    # And after it has been killed: the same stop, no longer running, said once.
    (found,) = pulse.left_behind(tmp_path)
    assert not found.running
    said = pulse.describe([found])
    assert "never came back" in said and "write to its terminal" in said and "kill" not in said
    assert "neither shows it" in said
    assert pulse.left_behind(tmp_path) == [], "handed over once"


def test_a_session_that_is_killed_is_reported_as_ended_without_closing(tmp_path):
    child = _child(tmp_path, "await asyncio.sleep(60)")
    try:
        _wait_for(lambda: list((tmp_path / pulse.DIR).glob("*.json")))
        time.sleep(0.3)
        assert pulse.left_behind(tmp_path, consume=False) == [], "running beside this one: not reported"
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait()
    (found,) = pulse.left_behind(tmp_path)
    assert found.pid == child.pid and found.stall is None and not found.running
    said = pulse.describe([found])
    assert "ended without closing at about" in said and "It was responding until then." in said
    assert "killed outright" in said and "stopped responding at" not in said


@pytest.mark.parametrize("name, word", [("SIGHUP", "its terminal went away"),
                                        ("SIGTERM", "something stopped it")])
def test_a_signal_that_ends_the_session_is_written_down_first(tmp_path, name, word):
    """The commonest end of a session driven from the web: the ssh window that
    held it closes, and the process is hung up with it."""
    child = _child(tmp_path, "await asyncio.sleep(60)")
    try:
        _wait_for(lambda: list((tmp_path / pulse.DIR).glob("*.json")))
        time.sleep(0.3)
        child.send_signal(getattr(signal, name))
        code = child.wait(timeout=10)
    finally:
        child.kill()
        child.wait()
    assert code == -getattr(signal, name), "and the signal still does what it was sent to do"
    (found,) = pulse.left_behind(tmp_path)
    assert found.ended_by == name
    said = pulse.describe([found])
    assert f"was ended from outside at" in said and f"by {name}" in said and word in said
    if name == "SIGHUP":
        assert "tmux or screen" in said


def test_a_handler_someone_else_installed_is_left_alone(tmp_path):
    seen = []
    mine = lambda number, frame: seen.append(number)       # noqa: E731
    before = signal.signal(signal.SIGHUP, mine)
    try:
        async def go():
            p = pulse.Pulse(tmp_path, **FAST)
            p.start()
            assert signal.getsignal(signal.SIGHUP) is mine
            assert signal.getsignal(signal.SIGTERM) == p._ended_by
            await p.stop()
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL, "and its own is taken back"
        asyncio.run(go())
    finally:
        signal.signal(signal.SIGHUP, before)


def _file(tmp_path, pid, **state) -> Path:
    folder = tmp_path / pulse.DIR
    folder.mkdir(parents=True, exist_ok=True)
    base = {"pid": pid, "host": socket.gethostname(), "started": "2026-11-17T12:33:00Z",
            "at": "2026-11-17T22:06:30Z", "stall": None, "stalls": 0}
    path = folder / f"{pid}.json"
    path.write_text(json.dumps({**base, **state}), encoding="utf-8")
    return path


NOW = pulse._parse("2026-11-18T08:19:12Z")


def test_what_is_and_is_not_a_session_that_did_not_end(tmp_path):
    fresh = pulse._iso(NOW - 20)
    _file(tmp_path, 101)                                              # gone, stamped ten hours ago
    _file(tmp_path, 102, at=fresh)                                    # running beside this one
    _file(tmp_path, 103)                                              # the number is in use, the file is not
    _file(tmp_path, 104, at=fresh, host="another-machine")            # another machine's, live
    _file(tmp_path, 105, host="another-machine")                      # another machine's, abandoned
    _file(tmp_path, os.getpid(), at=fresh)                            # this process
    (tmp_path / pulse.DIR / "junk.json").write_text("{not json", encoding="utf-8")
    found = pulse.left_behind(tmp_path, now=lambda: NOW, alive=lambda pid: pid in (102, 103))
    assert sorted(left.pid for left in found) == [101, 103, 105]
    assert not any(left.running for left in found)
    remaining = sorted(p.name for p in (tmp_path / pulse.DIR).glob("*.json"))
    assert remaining == sorted(["102.json", "104.json", f"{os.getpid()}.json"])


def test_the_most_recent_one_is_described_and_the_others_counted(tmp_path):
    stall = {"since": "2026-11-17T22:06:40Z", "noticed": "2026-11-17T22:07:10Z", "resumed": None,
             "where": ["repl.py:2400 run", "repl.py:2044 _say — print(text)"]}
    _file(tmp_path, 201, at="2026-11-17T22:06:30Z", stall=stall, stalls=1)
    _file(tmp_path, 202, at="2026-11-10T01:00:00Z")
    found = pulse.left_behind(tmp_path, now=lambda: NOW, alive=lambda pid: False)
    said = pulse.describe(found, now=lambda: NOW)
    assert "pid 201, started 11-17 12:33 UTC" in said
    assert "stopped responding at 11-17 22:06 UTC, 10 h 12 min ago and never came back" in said
    assert "repl.py:2044 _say — print(text) ← repl.py:2400 run" in said
    assert "write to its terminal" in said
    assert "(1 older session(s) of this project also did not end.)" in said


def test_a_stop_somewhere_else_is_not_called_a_terminal(tmp_path):
    stall = {"since": "2026-11-17T22:06:40Z", "noticed": "2026-11-17T22:07:10Z", "resumed": None,
             "where": ["control.py:930 _call", "selectors.py:468 select — fd_event_list = self._selector.poll(timeout)"]}
    _file(tmp_path, 301, stall=stall, stalls=1)
    said = pulse.describe(pulse.left_behind(tmp_path, now=lambda: NOW, alive=lambda pid: False),
                          now=lambda: NOW)
    assert "selectors.py:468" in said and "terminal" not in said


def test_a_stall_it_recovered_from_is_history_not_the_cause(tmp_path):
    stall = {"since": "2026-11-17T15:00:00Z", "noticed": "2026-11-17T15:00:35Z",
             "resumed": "2026-11-17T15:02:00Z", "where": ["x.py:1 f"]}
    _file(tmp_path, 401, stall=stall, stalls=2)
    said = pulse.describe(pulse.left_behind(tmp_path, now=lambda: NOW, alive=lambda pid: False),
                          now=lambda: NOW)
    assert "ended without closing at about 11-17 22:06 UTC" in said
    assert "stopped responding 2 time(s) before that and come back, last at 11-17 15:00 UTC" in said


def test_nothing_left_behind_is_nothing_said(tmp_path):
    assert pulse.left_behind(tmp_path) == []
    assert pulse.describe([]) == ""


def test_a_project_that_cannot_be_written_does_not_stop_the_session(tmp_path):
    blocked = tmp_path / ".scivo"
    blocked.write_text("a file where the folder would go", encoding="utf-8")

    async def go():
        p = pulse.Pulse(tmp_path, **FAST)
        p.start()
        await asyncio.sleep(0.15)
        await p.stop()
    asyncio.run(go())
    assert pulse.left_behind(tmp_path) == []


# --- where the record goes ---------------------------------------------------

def test_the_model_is_told_and_told_the_transcript_does_not_show_it():
    """The session that was asked "what happened?" answered from the
    transcript: nothing. The briefing is where it can be told otherwise."""
    from scivo_harness.preflight import Briefing, to_markdown
    said = "The session before this one (pid 7, started 11-17 12:33 UTC) stopped responding at 11-17 22:06 UTC."
    with_it = to_markdown(Briefing(identity={"project_id": "p", "previous_session": said}))
    assert said in with_it
    assert "did not end normally" in with_it and "the transcript does not show" in with_it
    without = to_markdown(Briefing(identity={"project_id": "p"}))
    assert "did not end normally" not in without


def test_the_session_takes_what_was_left_into_its_briefing_and_its_notes():
    import inspect
    from scivo_harness import session
    source = inspect.getsource(session.build)
    assert "pulse.left_behind(config.root)" in source
    assert 'briefing.identity["previous_session"] = before' in source
    assert "session.notes.append(before)" in source
    # A one-shot run does not eat a record meant for a person.
    assert source.index("if interactive:") < source.index("pulse.left_behind(config.root)")


def test_the_repl_keeps_the_record_for_exactly_as_long_as_it_runs(tmp_path):
    from types import SimpleNamespace
    from scivo_harness.repl import Repl

    seen = {}

    async def go(fail: bool):
        repl = Repl.__new__(Repl)
        repl.session = SimpleNamespace(config=SimpleNamespace(root=tmp_path))

        async def inner():
            seen["during"] = repl.pulse.path.exists()
            if fail:
                raise RuntimeError("an error with its traceback on the screen")
        repl._run = inner
        await repl.run()

    asyncio.run(go(False))
    assert seen["during"] is True
    assert pulse.left_behind(tmp_path) == []
    with pytest.raises(RuntimeError):
        asyncio.run(go(True))
    # Looked for by name: `left_behind` never reports the process that asks.
    assert list((tmp_path / pulse.DIR).glob("*.json")) == [], \
        "an error ended it from the inside; nothing to report"


def test_the_takeover_line_says_when_the_earlier_session_was_last_heard_from(monkeypatch):
    from scivo_harness import control
    monkeypatch.setattr(control, "_now_ms", lambda: 1_000_000_000)
    assert control._last_heard({"at": 1_000_000_000 - 36_720_000}) == \
        "; it was last heard from 10 h 12 min before this"
    assert control._last_heard({"at": 1_000_000_000 - 45_000}) == "; it was last heard from 45 s before this"
    assert control._last_heard({"at": 1_000_000_000 - 600_000}) == "; it was last heard from 10 min before this"
    assert control._last_heard({}) == "" and control._last_heard({"at": "soon"}) == ""


def test_the_takeover_line_reaches_the_page_not_only_the_terminal():
    import inspect
    from scivo_harness.repl import Repl
    source = inspect.getsource(Repl._control)
    assert 'self._say(ui.dim(f"  {self.control._claim_note}"))' in source
