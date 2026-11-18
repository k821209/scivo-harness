"""A session's own record of being alive, for the session that comes after it.

A session stopped answering overnight. The person found its screen frozen,
started it again, and asked the new session what had gone wrong (feedback
515b505746ee, 2026-11-18). The new session read the transcript and reported,
correctly, that it showed nothing: every turn had finished, the last one ten
hours before. The transcript is written by the model's side of the session. A
harness whose own loop has stopped writes nothing anywhere, so from inside
the conversation a hung session and an idle one are the same file.

The page could not say either: the restart clears the earlier session's log,
and with it the last time that session had been heard from. Nothing was left
on the machine or on the server to tell a stuck process from a closed window.

So a running session keeps one small file, `.scivo/pulse/<pid>.json`:

* the loop stamps it every `SAVE_EVERY` seconds, so the file says until when
  the loop was running;
* a thread watches the loop. A loop that has not turned for `STALL_AFTER`
  seconds is recorded there with WHERE the main thread is standing — which is
  the one fact that tells "blocked writing to a terminal nobody is reading"
  from anything else;
* a signal that ends the session — the terminal going away, a `kill` — is
  written down on the way out, because that is the commonest way a session
  driven from the web dies: its ssh window closes, or the laptop that held it
  sleeps, and the process is hung up with the connection;
* a session that ends takes its file with it. A file whose process is gone is
  therefore a session that did not end, and the next session in the project
  says so, once: to the person, on the page, and in the model's briefing, so
  "what happened?" has an answer.

The record is written locally and holds times, a process id and source
positions — never a prompt, a path the person typed, or output.

One thing here does change what a session does, and only a session that has
already stopped. A terminal that is not reading takes about 8 KB (measured,
Linux pty) and then every `print` waits — in the loop's own thread, so the
heartbeat, the inbox and the turn wait with it. One long reply is enough.
Paused output (Ctrl-S), a frozen window, a connection that has died without
saying so: the session is driven from the web, and it is stopped by a window
nobody is looking at. When the loop has stood for `STALL_AFTER` seconds, its
terminal is not taking output, and `may_let_go()` says someone can reach the
session elsewhere (a control page is up), the watching thread points the
terminal's descriptors at nothing and wakes the write; it is retried, goes
nowhere, and the loop turns again. The terminal is kept, and is given its
output back the moment it reads again. Without a page nothing is let go: a
person at a paused terminal is owed the pause.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import select
import signal
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DIR = Path(".scivo") / "pulse"
TICK = 1.0            # how often the loop says it turned
SAVE_EVERY = 30.0     # how often that reaches the file
STALL_AFTER = 30.0    # a loop silent this long is not busy, it is stopped
WATCH_EVERY = 5.0
# A file its loop has not stamped for this long belongs to no running session,
# whatever a process with the same number is doing now.
ABANDONED_AFTER = 300.0
FRAMES = 8
# The signals that end a session from outside and can be written down first.
# SIGKILL cannot; that one is told from the file being left with no word in it.
FATAL = tuple(getattr(signal, name) for name in ("SIGHUP", "SIGTERM") if hasattr(signal, name))
# Sent to the loop's thread to interrupt a write that is waiting on a terminal.
WAKE = getattr(signal, "SIGUSR1", None)


def _taking_output(descriptor: int) -> bool:
    """Would a write to this descriptor go through now?"""
    try:
        return bool(select.select([], [descriptor], [], 0)[1])
    except (OSError, ValueError):
        return True                  # not something we can judge: leave it alone


def _still_a_terminal(descriptor: int) -> bool:
    """A terminal that has been hung up polls as writable and fails on write."""
    try:
        import termios
        termios.tcgetattr(descriptor)
        return True
    except Exception:  # noqa: BLE001 — termios.error, OSError, no termios at all
        return False


def _iso(when: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def _parse(stamp: Any) -> float | None:
    try:
        return float(__import__("calendar").timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def where(frame) -> list[str]:
    """The innermost frames of a thread, outermost first: `file:line function`,
    with the source line for the last two."""
    stack = traceback.extract_stack(frame)[-FRAMES:]
    out = []
    for index, entry in enumerate(stack):
        text = f"{Path(entry.filename).name}:{entry.lineno} {entry.name}"
        if index >= len(stack) - 2 and entry.line:
            text += f" — {entry.line.strip()[:80]}"
        out.append(text)
    return out


@dataclass
class Pulse:
    root: Path
    now: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    stall_after: float = STALL_AFTER
    watch_every: float = WATCH_EVERY
    tick: float = TICK
    save_every: float = SAVE_EVERY
    _state: dict[str, Any] = field(default_factory=dict)
    _ticked: float = 0.0
    _saved: float = 0.0
    _stalled: bool = False
    _task: asyncio.Task | None = None
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _loop_thread: int = 0
    _handlers: dict = field(default_factory=dict)
    # Whether someone can reach this session without its terminal. Asked from
    # the watching thread, so it must only read.
    may_let_go: Callable[[], bool] = lambda: False
    # Called in the loop: (when output stopped) and (stopped, came back).
    on_let_go: Callable[[str], None] | None = None
    on_back: Callable[[str, str], None] | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _held: int = -1                  # the terminal, kept while output goes nowhere
    _redirected: tuple = ()
    _let_go_at: str = ""
    _armed: bool = False

    @property
    def path(self) -> Path:
        return self.root / DIR / f"{os.getpid()}.json"

    def start(self) -> None:
        """Call from the running loop, in the thread that runs it."""
        self._loop_thread = threading.get_ident()
        self._ticked = self.monotonic()
        self._state = {"pid": os.getpid(), "host": socket.gethostname(),
                       "started": _iso(self.now()), "at": _iso(self.now()),
                       "stall": None, "stalls": 0}
        self._save()
        self._task = asyncio.get_running_loop().create_task(self._beat(), name="scivo-pulse")
        self._thread = threading.Thread(target=self._watch, name="scivo-pulse-watch", daemon=True)
        self._thread.start()
        self._loop = asyncio.get_running_loop()
        for number in FATAL:
            # Only where nothing else has asked for the signal, and only from
            # the main thread, which is the one place Python allows it.
            with contextlib.suppress(ValueError, OSError):
                if signal.getsignal(number) is signal.SIG_DFL:
                    self._handlers[number] = signal.signal(number, self._ended_by)
        # The wake-up for a write that is waiting on the terminal. With no
        # handler of ours in place the signal would end the process, so
        # letting go is armed only when this took.
        with contextlib.suppress(ValueError, OSError):
            if WAKE is not None and signal.getsignal(WAKE) is signal.SIG_DFL:
                self._handlers[WAKE] = signal.signal(WAKE, lambda _number, _frame: None)
                self._armed = True

    def _ended_by(self, number: int, _frame) -> None:
        """Write the signal down, then let it do what it was sent to do."""
        with contextlib.suppress(Exception):
            if self._lock.acquire(timeout=1.0):
                try:
                    self._state["ended_by"] = signal.Signals(number).name
                    self._state["at"] = _iso(self.now())
                finally:
                    self._lock.release()
                self._save()
        signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self.tick)
            self._ticked = self.monotonic()
            if self._ticked - self._saved >= self.save_every:
                with self._lock:
                    self._state["at"] = _iso(self.now())
                self._save()

    def _watch(self) -> None:
        while not self._stop.wait(self.watch_every):
            self._look()

    def _look(self) -> None:
        """One look at the loop, from the watching thread."""
        quiet = self.monotonic() - self._ticked
        if quiet >= self.stall_after and not self._stalled:
            self._stalled = True
            frame = sys._current_frames().get(self._loop_thread)
            with self._lock:
                self._state["stalls"] = int(self._state.get("stalls") or 0) + 1
                self._state["stall"] = {
                    # When the loop last turned, not when this thread noticed.
                    "since": _iso(self.now() - quiet), "noticed": _iso(self.now()),
                    "where": where(frame) if frame is not None else [], "resumed": None}
            self._save()
        elif self._stalled and quiet < self.stall_after:
            self._stalled = False
            with self._lock:
                if self._state.get("stall"):
                    self._state["stall"]["resumed"] = _iso(self.now())
            self._save()
        if self._stalled and self._held < 0:
            self._let_go()
        elif self._held >= 0 and not self._stalled:
            self._take_back()

    def _let_go(self) -> None:
        """The loop is standing on a terminal that takes no output, and the
        session can be reached elsewhere: stop writing to the terminal."""
        if not self._armed or not self.may_let_go():
            return
        try:
            if not os.isatty(1) or _taking_output(1):
                return
            device = os.fstat(1).st_rdev
            held = os.dup(1)
            nowhere = os.open(os.devnull, os.O_WRONLY)
            redirected = []
            for descriptor in (1, 2):
                with contextlib.suppress(OSError):
                    if os.isatty(descriptor) and os.fstat(descriptor).st_rdev == device:
                        os.dup2(nowhere, descriptor)
                        redirected.append(descriptor)
            os.close(nowhere)
        except OSError:
            return
        self._held, self._redirected = held, tuple(redirected)
        self._let_go_at = _iso(self.now())
        with self._lock:
            self._state["let_go"] = {"at": self._let_go_at, "back": None}
        self._save()
        # The write is waiting inside the kernel on the OLD terminal. The
        # signal interrupts it; Python runs the (empty) handler and tries the
        # write again on the same descriptor number, which now leads nowhere.
        with contextlib.suppress(Exception):
            signal.pthread_kill(self._loop_thread, WAKE)
        self._tell(self.on_let_go, self._let_go_at)

    def _take_back(self) -> None:
        """The terminal is reading again: it gets its output back."""
        if not _still_a_terminal(self._held) or not _taking_output(self._held):
            return
        held, redirected, since = self._held, self._redirected, self._let_go_at
        try:
            for descriptor in redirected:
                os.dup2(held, descriptor)
            os.close(held)
        except OSError:
            return
        self._held, self._redirected = -1, ()
        back = _iso(self.now())
        with self._lock:
            if self._state.get("let_go"):
                self._state["let_go"]["back"] = back
        self._save()
        self._tell(self.on_back, since, back)

    def _tell(self, callback, *args) -> None:
        if callback is None or self._loop is None:
            return
        with contextlib.suppress(RuntimeError):          # the loop has closed
            self._loop.call_soon_threadsafe(callback, *args)

    def _save(self) -> None:
        # Never the reason a session fails: a read-only project, a full disk.
        with contextlib.suppress(OSError):
            with self._lock:
                text = json.dumps(self._state, indent=1)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            scratch = self.path.with_suffix(f".{threading.get_ident()}.tmp")
            scratch.write_text(text + "\n", encoding="utf-8")
            os.replace(scratch, self.path)
            self._saved = self.monotonic()

    async def stop(self) -> None:
        """The session is ending as it should: nothing is left behind."""
        self._stop.set()
        for number, previous in self._handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(number, previous)
        self._handlers = {}
        self._armed = False
        if self._held >= 0:
            # Ending while let go: the terminal is handed back as it was, so
            # the shell that follows this process is not left writing nowhere.
            with contextlib.suppress(OSError):
                for descriptor in self._redirected:
                    os.dup2(self._held, descriptor)
                os.close(self._held)
            self._held, self._redirected = -1, ()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        with contextlib.suppress(OSError):
            self.path.unlink()


@dataclass(frozen=True)
class Left:
    """What one session that did not end left behind."""
    pid: int
    host: str
    started: str
    at: str
    stall: dict | None
    stalls: int
    running: bool        # its process is still there, and stopped
    ended_by: str = ""   # the signal that ended it, when it could be written down


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def left_behind(root: Path, *, now: Callable[[], float] = time.time,
                alive: Callable[[int], bool] = _alive, consume: bool = True) -> list[Left]:
    """Sessions of this project that stopped without ending, most recent first.

    A record is handed over once: its file goes when it has been read, unless
    the process is still there — that one is reported again by the next
    session too, for as long as it stands."""
    out: list[tuple[float, Left]] = []
    folder = root / DIR
    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        return []
    for path in files:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            pid = int(state["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            with contextlib.suppress(OSError):
                if consume:
                    path.unlink()
            continue
        if pid == os.getpid():
            continue
        stamped = _parse(state.get("at")) or 0.0
        stall = state.get("stall") if isinstance(state.get("stall"), dict) else None
        stopped = bool(stall and not stall.get("resumed"))
        here = state.get("host") == socket.gethostname()
        there = here and alive(pid)
        fresh = now() - stamped < ABANDONED_AFTER
        if there and fresh and not stopped:
            continue                                  # a session running beside this one
        if not here and fresh:
            continue                                  # another machine's, still being stamped
        running = there and stopped
        out.append((stamped, Left(pid=pid, host=str(state.get("host") or ""),
                                  started=str(state.get("started") or ""),
                                  at=str(state.get("at") or ""), stall=stall,
                                  stalls=int(state.get("stalls") or 0), running=running,
                                  ended_by=str(state.get("ended_by") or ""))))
        if consume and not running:
            with contextlib.suppress(OSError):
                path.unlink()
    return [left for _stamp, left in sorted(out, key=lambda pair: pair[0], reverse=True)]


def _clock(stamp: str) -> str:
    """`11-18 07:07 UTC` — the day matters: these are found the morning after."""
    when = _parse(stamp)
    return time.strftime("%m-%d %H:%M UTC", time.gmtime(when)) if when is not None else "an unknown time"


def _span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


_TERMINAL = ("print(", "stdout", ".write(", "flush(")
_SIGNALS = {
    "SIGHUP": (" — its terminal went away: the window was closed, or the connection that held "
               "it (ssh from a machine that slept or lost its network) was dropped. A session "
               "that has to outlive its terminal is started inside tmux or screen."),
    "SIGTERM": " — something stopped it: a `kill`, a shutdown, a job scheduler.",
}


def describe(left: list[Left], *, now: Callable[[], float] = time.time) -> str:
    """What to say about them, in one paragraph. Empty when there is nothing."""
    if not left:
        return ""
    first = left[0]
    who = f"pid {first.pid}, started {_clock(first.started)}"
    stall = first.stall if first.stall and not first.stall.get("resumed") else None
    if stall:
        since = _parse(stall.get("since"))
        ago = f", {_span(now() - since)} ago" if since is not None else ""
        frames = [str(row) for row in (stall.get("where") or [])]
        text = (f"The session before this one ({who}) stopped responding at "
                f"{_clock(str(stall.get('since')))}{ago}"
                + (" and is STILL RUNNING, stopped" if first.running else " and never came back")
                + ". Nothing after that reached the page or the transcript, so neither shows it.")
        if frames:
            text += " Its main thread was standing in: " + " ← ".join(reversed(frames[-4:])) + "."
            if any(mark in frame for frame in frames[-2:] for mark in _TERMINAL):
                text += (" That is a write to its terminal which the terminal was not taking: "
                         "a window that was closed or frozen, output paused (Ctrl-S), or a "
                         "connection that had died.")
        if first.running:
            text += f" It will not recover by itself: `kill {first.pid}` ends it."
        elif first.ended_by:
            text += f" It was ended by {first.ended_by} at {_clock(first.at)}."
    elif first.ended_by:
        text = (f"The session before this one ({who}) was ended from outside at "
                f"{_clock(first.at)} by {first.ended_by}"
                + _SIGNALS.get(first.ended_by, "")
                + " It was responding until then; nothing after that reached the page.")
    else:
        text = (f"The session before this one ({who}) ended without closing at about "
                f"{_clock(first.at)} — it was killed outright, or the machine went down. "
                "It was responding until then.")
        if first.stalls:
            text += (f" It had stopped responding {first.stalls} time(s) before that and come back"
                     + (f", last at {_clock(str(first.stall.get('since')))}" if first.stall else "")
                     + ".")
    if len(left) > 1:
        text += f" ({len(left) - 1} older session(s) of this project also did not end.)"
    return text
