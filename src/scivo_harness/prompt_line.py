"""The input line: slash completion, history, editing.

`input()` was enough to prove the loop works, but the command surface here is 28
skills plus the local commands, and the only way to learn it was to read the
guide. A completion menu turns that into something you discover by pressing `/`.

Falls back to `input()` when stdin is not a terminal, so piping a script into
`scivo` keeps working — prompt_toolkit raises on a non-tty rather than degrading.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable

from .skills import Skill


class _SlashCompleter:
    """Completes `/name` against local commands and installed skills."""

    def __init__(self, commands: dict[str, str], skills: list[Skill]) -> None:
        from prompt_toolkit.completion import Completer

        self._entries: list[tuple[str, str, str]] = [
            *((name, "command", help_text) for name, help_text in commands.items()),
            *((f"/{skill.name}", "skill", skill.summary) for skill in skills),
        ]
        self._Completer = Completer  # noqa: N803 - stored for the adapter below

    def entries(self, word: str) -> Iterable[tuple[str, str, str]]:
        needle = word.lstrip("/").lower()
        starts = [e for e in self._entries if e[0].lstrip("/").lower().startswith(needle)]
        # Substring matches after prefix matches: /revision should find
        # /paper-revision and /video-revision without knowing the prefix.
        contains = [e for e in self._entries
                    if needle and needle in e[0].lower() and e not in starts]
        return starts + contains


def _build_completer(commands: dict[str, str], skills: list[Skill]) -> Any:
    from prompt_toolkit.completion import Completer, Completion

    inner = _SlashCompleter(commands, skills)

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):  # noqa: ANN001
            text = document.text_before_cursor
            # Only the first word, and only when it is a slash command.
            if not text.startswith("/") or " " in text:
                return
            for name, kind, summary in inner.entries(text):
                yield Completion(
                    name,
                    start_position=-len(text),
                    display=name,
                    display_meta=f"{kind} · {summary}" if summary else kind,
                )

    return SlashCompleter()


class Line:
    """One prompt, reading with completion where the terminal allows it."""

    def __init__(self, root: Path, commands: dict[str, str], skills: list[Skill]) -> None:
        self._session = None
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory
        except ImportError:
            return

        history_file = root / ".scivo" / "history"
        try:
            history_file.parent.mkdir(parents=True, exist_ok=True)
            history: Any = FileHistory(str(history_file))
        except OSError:
            history = None

        self._session = PromptSession(
            completer=_build_completer(commands, skills),
            history=history,
            complete_while_typing=True,
            reserve_space_for_menu=9,
        )

    @property
    def rich(self) -> bool:
        return self._session is not None

    async def ask(self, styled: str, plain: str) -> str:
        """`styled` may carry ANSI; `plain` is the fallback for a dumb stdin."""
        if self._session is None:
            return await _thread_input(plain)
        from prompt_toolkit.formatted_text import ANSI

        return await self._session.prompt_async(ANSI(styled))


async def _thread_input(plain_text: str) -> str:
    import asyncio

    return await asyncio.to_thread(input, plain_text)
