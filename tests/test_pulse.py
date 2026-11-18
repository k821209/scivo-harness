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


# --- a terminal that takes no output must not stop a session that has a page ---

TALKER = textwrap.dedent("""
    import asyncio, sys
    sys.path.insert(0, {src!r})
    from pathlib import Path
    from scivo_harness import pulse

    root = Path({root!r})

    async def main():
        p = pulse.Pulse(root, tick=0.02, save_every=0.05, stall_after=0.4, watch_every=0.05,
                        may_let_go=lambda: {page},
                        on_let_go=lambda at: (root / "let_go").write_text(at),
                        on_back=lambda since, back: (root / "back").write_text(since + " " + back))
        p.start()
        n = 0
        while not (root / "enough").exists():
            n += 1
            print(f"line {{n:06d}} " + "x" * 180, flush=True)      # what a turn does: it prints
            (root / "turning").write_text(str(n))
            if n == 30 and {nap}:
                __import__("time").sleep({nap})                      # the loop stands, for another reason
            await asyncio.sleep(0.005)
        await p.stop()

    asyncio.run(main())
""")


def _talker(tmp_path, page: bool, nap: float = 0):
    import pty
    script = tmp_path / "talker.py"
    script.write_text(TALKER.format(src=SRC, root=str(tmp_path), page=page, nap=nap), encoding="utf-8")
    master, slave = pty.openpty()
    child = subprocess.Popen([sys.executable, str(script)], stdin=slave, stdout=slave, stderr=slave,
                             start_new_session=True)
    os.close(slave)
    return child, master


def _turning(tmp_path) -> int:
    try:
        return int((tmp_path / "turning").read_text() or 0)
    except (OSError, ValueError):
        return -1


def _read_some(master, seconds: float) -> str:
    import select
    out, end = b"", time.time() + seconds
    while time.time() < end:
        if select.select([master], [], [], 0.05)[0]:
            try:
                out += os.read(master, 65536)
            except OSError:
                break
    return out.decode("utf-8", "replace")


def _finish(tmp_path, child, master) -> None:
    (tmp_path / "enough").write_text("")
    deadline = time.time() + 5
    while child.poll() is None and time.time() < deadline:
        _read_some(master, 0.1)
    if child.poll() is None:
        child.kill()
    child.wait()
    os.close(master)


def test_a_terminal_nobody_reads_takes_about_eight_kilobytes(tmp_path):
    """The size of the trap, measured rather than remembered: one long reply."""
    import fcntl
    import pty
    master, slave = pty.openpty()
    try:
        fcntl.fcntl(slave, fcntl.F_SETFL, fcntl.fcntl(slave, fcntl.F_GETFL) | os.O_NONBLOCK)
        taken = 0
        with pytest.raises(BlockingIOError):
            while taken < 4_000_000:
                taken += os.write(slave, b"x" * 512 + b"\n")
        assert 2_000 < taken < 200_000, taken
    finally:
        os.close(master)
        os.close(slave)


def test_a_session_with_a_page_lets_go_of_a_terminal_that_is_not_read_and_carries_on(tmp_path):
    child, master = _talker(tmp_path, page=True)
    try:
        _wait_for(lambda: (tmp_path / "let_go").exists(), seconds=15)       # nobody read; it let go
        first = _turning(tmp_path)
        time.sleep(0.5)
        assert _turning(tmp_path) > first + 10, "the loop turns again while the window is still unread"
        state = json.loads((tmp_path / pulse.DIR / f"{child.pid}.json").read_text())
        assert state["let_go"]["at"] and state["let_go"]["back"] is None
        assert state["stall"]["where"], "and the stop that led to it is on record"
        assert not (tmp_path / "back").exists()
        # The window is read again: it gets its output back, from where the session now is.
        seen = _read_some(master, 0.3)
        _wait_for(lambda: (tmp_path / "back").exists(), seconds=10)
        later = _read_some(master, 0.6)
        numbers = [int(part.split()[1]) for part in (seen + later).split("\n") if part.startswith("line ")
                   and part.split()[1].isdigit()]
        assert numbers and max(numbers) > first, "new lines reach the terminal after it comes back"
        gaps = [b - a for a, b in zip(numbers, numbers[1:])]
        assert max(gaps) > 10, "and what was printed in between went nowhere, not into a queue"
        state = json.loads((tmp_path / pulse.DIR / f"{child.pid}.json").read_text())
        assert state["let_go"]["back"] and state["stall"]["resumed"]
    finally:
        _finish(tmp_path, child, master)
    assert child.returncode == 0
    assert pulse.left_behind(tmp_path) == []


def test_without_a_page_a_paused_terminal_pauses_the_session_as_it_always_did(tmp_path):
    child, master = _talker(tmp_path, page=False)
    try:
        def stalled():
            files = list((tmp_path / pulse.DIR).glob("*.json"))
            return files and json.loads(files[0].read_text()).get("stall")
        _wait_for(stalled, seconds=15)
        first = _turning(tmp_path)
        time.sleep(0.6)
        assert _turning(tmp_path) == first, "nobody could have seen it carry on, so it waits"
        assert not (tmp_path / "let_go").exists()
        _read_some(master, 0.5)                                            # the person reads again
        _wait_for(lambda: _turning(tmp_path) > first, seconds=10)
    finally:
        _finish(tmp_path, child, master)
    assert child.returncode == 0


def test_output_paused_with_ctrl_s_is_let_go_and_taken_back_on_ctrl_q(tmp_path):
    import threading
    child, master = _talker(tmp_path, page=True)
    reading = threading.Event()
    reading.set()
    done = threading.Event()

    def window():                                   # a window that IS read, like any terminal
        while not done.is_set():
            if reading.is_set():
                _read_some(master, 0.05)
            else:
                time.sleep(0.05)
    reader = threading.Thread(target=window, daemon=True)
    reader.start()
    try:
        _wait_for(lambda: _turning(tmp_path) > 20, seconds=15)
        os.write(master, b"\x13")                                           # Ctrl-S
        _wait_for(lambda: (tmp_path / "let_go").exists(), seconds=15)
        first = _turning(tmp_path)
        time.sleep(0.4)
        assert _turning(tmp_path) > first + 10
        os.write(master, b"\x11")                                           # Ctrl-Q
        _wait_for(lambda: (tmp_path / "back").exists(), seconds=10)
    finally:
        done.set()
        reader.join(timeout=2)
        _finish(tmp_path, child, master)
    assert child.returncode == 0


def test_a_loop_that_stands_for_another_reason_keeps_its_terminal(tmp_path):
    """Letting go is for a terminal that takes no output — not for any stop.
    A window that is being read stays this session's window."""
    import threading
    child, master = _talker(tmp_path, page=True, nap=1.2)
    done = threading.Event()
    seen = []

    def window():
        while not done.is_set():
            seen.append(_read_some(master, 0.05))
    reader = threading.Thread(target=window, daemon=True)
    reader.start()
    try:
        def stalled_and_back():
            files = list((tmp_path / pulse.DIR).glob("*.json"))
            stall = (json.loads(files[0].read_text()).get("stall") if files else None) or {}
            return stall.get("resumed")
        _wait_for(stalled_and_back, seconds=15)
        state = json.loads((tmp_path / pulse.DIR / f"{child.pid}.json").read_text())
        assert "let_go" not in state and not (tmp_path / "let_go").exists()
        mark = _turning(tmp_path)
        _wait_for(lambda: f"line {mark + 5:06d}" in "".join(seen), seconds=10)   # still printing HERE
    finally:
        done.set()
        reader.join(timeout=2)
        _finish(tmp_path, child, master)
    assert child.returncode == 0


def test_letting_go_is_not_armed_when_the_wake_signal_is_someone_elses(tmp_path):
    mine = lambda number, frame: None                # noqa: E731
    before = signal.signal(signal.SIGUSR1, mine)
    try:
        async def go():
            p = pulse.Pulse(tmp_path, may_let_go=lambda: True, **FAST)
            p.start()
            armed, handler = p._armed, signal.getsignal(signal.SIGUSR1)
            await p.stop()
            return armed, handler
        armed, handler = asyncio.run(go())
        assert armed is False and handler is mine
    finally:
        signal.signal(signal.SIGUSR1, before)


def test_the_repl_lets_go_only_while_its_page_is_up_and_says_so_in_both_places():
    import inspect
    from scivo_harness.repl import Repl
    source = inspect.getsource(Repl.run)
    assert "may_let_go=self._page_is_up" in source
    assert "on_let_go=self._terminal_let_go" in source and "on_back=self._terminal_back" in source
    from types import SimpleNamespace
    repl = Repl.__new__(Repl)
    repl.control = None
    assert repl._page_is_up() is False
    repl.control = SimpleNamespace(active=False, taken_by="")
    assert repl._page_is_up() is False
    repl.control = SimpleNamespace(active=True, taken_by="")
    assert repl._page_is_up() is True
    repl.control = SimpleNamespace(active=True, taken_by="another-session")
    assert repl._page_is_up() is False, "a page another session took is not this one's to be reached on"
    said = []
    repl.control = SimpleNamespace(active=True, taken_by="",
                                   status=lambda text, level="info": said.append((level, text)))
    repl._terminal_let_go("2026-11-17T22:06:40Z")
    assert said and said[0][0] == "warn" and "22:06 UTC" in said[0][1]
    assert "stopped taking output" in said[0][1] and "carries on here" in said[0][1]
    printed = []
    repl._say = printed.append
    repl._terminal_back("2026-11-17T22:06:40Z", "2026-11-18T08:19:00Z")
    assert "took no output from 11-17 22:06 UTC to 11-18 08:19 UTC" in printed[0]
    assert "on the page and in the transcript, not here" in printed[0]
