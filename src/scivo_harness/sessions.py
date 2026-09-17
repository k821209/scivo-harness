"""Finding a session to resume without knowing its id.

`--resume` took a session id and nothing printed one, so resuming meant
reading ~/.claude/projects by hand. The Agent SDK can list a directory's
sessions with the title Claude Code gave each, so the harness shows those and
lets a session be named by its row number or the first few characters of its id.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import list_sessions


class SessionLookupError(RuntimeError):
    pass


@dataclass
class Row:
    index: int
    session_id: str
    when: str
    label: str


def rows(root: Path, limit: int | None = 20) -> list[Row]:
    found = list_sessions(directory=str(root), limit=limit)
    out = []
    for index, info in enumerate(found, start=1):
        when = datetime.datetime.fromtimestamp(info.last_modified / 1000).strftime("%m-%d %H:%M")
        label = info.custom_title or info.summary or info.first_prompt or ""
        out.append(Row(index, info.session_id, when, " ".join(str(label).split())[:70]))
    return out


def resolve(root: Path, token: str) -> str:
    """A row number from `scivo sessions`, a unique id prefix, or a full id."""
    token = token.strip()
    everything = rows(root, limit=None)
    if not everything:
        raise SessionLookupError(f"No sessions recorded for {root} yet.")
    if token.isdigit() and len(token) <= 3:
        number = int(token)
        if 1 <= number <= len(everything):
            return everything[number - 1].session_id
        raise SessionLookupError(f"There is no session #{number}; `scivo sessions` lists {len(everything)}.")
    matches = [row for row in everything if row.session_id.startswith(token)]
    if len(matches) == 1:
        return matches[0].session_id
    if not matches:
        raise SessionLookupError(f"No session id starts with {token!r}. See `scivo sessions`.")
    listed = ", ".join(row.session_id[:12] for row in matches[:5])
    raise SessionLookupError(f"{token!r} matches {len(matches)} sessions ({listed}); give more of the id.")


def latest(root: Path) -> str | None:
    found = rows(root, limit=1)
    return found[0].session_id if found else None
