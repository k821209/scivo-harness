"""Leaving scivo must hand back a terminal that shows what you type.

`quiet_echo()` wraps the whole session, and `os.execv` — the `/update`
relaunch, and `scivo login` — replaces the process without unwinding a single
`with`, so its restore never runs. The process we exec into then saves the
already-quiet terminal as ITS baseline and faithfully restores ECHO-off when
the user finally leaves. One relaunch poisoned every later exit, and the
symptom is the hardest kind to report: typing works and is invisible
(user, 2026-11-08).
"""
from __future__ import annotations

import sys
import types

import pytest

from scivo_harness import interrupts


class FakeTermios:
    """Just the four names used, over one mutable lflag."""
    ECHO = 0b0001
    ICANON = 0b0010
    TCSADRAIN = 1

    def __init__(self, lflag: int):
        self.attrs = [0, 0, 0, lflag, 0, 0, []]
        self.writes = 0

    def tcgetattr(self, _fd):
        return list(self.attrs)

    def tcsetattr(self, _fd, _when, attrs):
        self.writes += 1
        self.attrs = list(attrs)


@pytest.fixture
def tty(monkeypatch):
    def install(lflag):
        fake = FakeTermios(lflag)
        monkeypatch.setitem(sys.modules, "termios", fake)
        monkeypatch.setattr(interrupts.sys, "stdin",
                            types.SimpleNamespace(isatty=lambda: True,
                                                  fileno=lambda: 0))
        return fake
    return install


def test_it_turns_echo_back_on(tty):
    fake = tty(FakeTermios.ICANON)          # echo off, as a session leaves it
    assert interrupts.restore_terminal() is True
    assert fake.attrs[3] & FakeTermios.ECHO, "a shell prompt needs to echo"


def test_it_restores_line_mode_too(tty):
    fake = tty(0)                            # raw-ish: no echo, no canon
    assert interrupts.restore_terminal() is True
    assert fake.attrs[3] & FakeTermios.ECHO
    assert fake.attrs[3] & FakeTermios.ICANON


def test_a_healthy_terminal_is_not_touched(tty):
    fake = tty(FakeTermios.ECHO | FakeTermios.ICANON)
    assert interrupts.restore_terminal() is False, "nothing to fix"
    assert fake.writes == 0, "and it must not write for the sake of writing"


def test_it_says_whether_it_had_to_act(tty):
    """The return value is the honest part: a terminal fixed silently and one
    that needed nothing are different events."""
    assert interrupts.restore_terminal.__doc__
    assert tty(FakeTermios.ICANON) and interrupts.restore_terminal() is True
    assert interrupts.restore_terminal() is False, "already fixed by the first call"


def test_off_a_terminal_it_does_nothing(monkeypatch):
    monkeypatch.setattr(interrupts.sys, "stdin",
                        types.SimpleNamespace(isatty=lambda: False))
    assert interrupts.restore_terminal() is False


def test_a_termios_that_refuses_is_not_fatal(tty, monkeypatch):
    fake = tty(FakeTermios.ICANON)
    def refuse(*_a, **_k):
        raise OSError("not a tty after all")
    monkeypatch.setattr(fake, "tcsetattr", refuse)
    assert interrupts.restore_terminal() is False


def test_both_exec_sites_hand_the_terminal_back():
    """An exec unwinds nothing, so the call has to be explicit at each one."""
    import inspect

    from scivo_harness import cli
    source = inspect.getsource(cli)
    # Every os.execv in the module is preceded by a restore, within a few lines.
    lines = source.splitlines()
    execs = [n for n, line in enumerate(lines) if "os.execv(" in line]
    assert execs, "the exec sites moved — this check must be updated with them"
    for n in execs:
        window = "\n".join(lines[max(0, n - 6):n])
        assert "restore_terminal()" in window, f"os.execv at line {n + 1}"


def test_the_cli_registers_the_last_resort_guard():
    import inspect

    from scivo_harness import cli
    body = inspect.getsource(cli.main)
    assert "atexit.register(restore_terminal)" in body
