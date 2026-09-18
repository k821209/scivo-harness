"""Esc stops the turn that is running.

Ctrl-C already did, but it is the key people press to leave a program, so it
was the one thing nobody tried on a turn they only wanted to stop. Esc is what
Claude Code itself uses, and a local model that has decided to call the same
tool eleven times gives you plenty of time to reach for it.

While a turn runs nothing is reading stdin, so the keys pressed during it sit
in the terminal buffer and land in the next prompt. This reads them instead:
Esc interrupts, and anything else is handed back so the next prompt opens with
it already typed, which is where the person thought it was going.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Callable


class KeyWatcher:
    """Raw-mode stdin for the duration of a turn. A no-op off a terminal."""

    def __init__(self, on_interrupt: Callable[[], None]) -> None:
        self._on_interrupt = on_interrupt
        self._fd: int | None = None
        self._saved = None
        self.typed_ahead = ""
        self.interrupted = False

    def __enter__(self) -> "KeyWatcher":
        try:
            if not sys.stdin.isatty():
                return self
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            asyncio.get_running_loop().add_reader(self._fd, self._read)
        except Exception:  # noqa: BLE001 - a terminal we cannot put in raw mode still works
            self._restore()
        return self

    def __exit__(self, *exception: object) -> None:
        self._restore()

    def _restore(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(fd)
        except Exception:  # noqa: BLE001
            pass
        if self._saved is not None:
            import contextlib
            import termios

            with contextlib.suppress(Exception):
                termios.tcsetattr(fd, termios.TCSADRAIN, self._saved)
            self._saved = None

    def _read(self) -> None:
        try:
            data = os.read(self._fd, 1024) if self._fd is not None else b""
        except OSError:
            return
        if not data:
            return
        # A bare Esc is the stop. Esc followed by anything is an arrow key or
        # another escape sequence, which is someone editing, not stopping.
        if data == b"\x1b":
            if not self.interrupted:
                self.interrupted = True
                self._on_interrupt()
            return
        if data.startswith(b"\x1b"):
            return
        self.typed_ahead += data.decode("utf-8", "replace")
