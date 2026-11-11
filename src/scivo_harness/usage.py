"""Skill invocations, into the same local tally the MCP keeps of tool calls.

The MCP counts every tool call it dispatches (`co_scientist_local/usage.py`).
A skill never reaches it: the host reads the SKILL.md and the model carries
on. So of the two questions the tally exists for — which tools, which skills —
the server can answer one, and this session answers the other.

Same file, same line, so one report reads both:

    2026-11-11T06:12:03Z <TAB> skill <TAB> paper-writing <TAB> <project id> <TAB> <host>

The name and nothing else. `SCIVO_USAGE=off` turns it off; it never raises.
The format is the MCP's — if it changes there, it changes here.
"""
from __future__ import annotations

import datetime as _dt
import os
import pathlib
import socket

_OFF = ("off", "0", "no", "false")


def enabled() -> bool:
    return (os.environ.get("SCIVO_USAGE", "").strip().lower() or "on") not in _OFF


def directory() -> pathlib.Path:
    override = os.environ.get("SCIVO_USAGE_DIR")
    return pathlib.Path(override) if override else pathlib.Path.home() / ".co-scientist" / "usage"


def record_skill(name: str, project: str = "") -> None:
    if not enabled() or not name:
        return
    try:
        when = _dt.datetime.now(_dt.timezone.utc)
        clean = lambda s: str(s).replace("\t", " ").replace("\n", " ")[:120]  # noqa: E731
        folder = directory()
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / f"{when:%Y-%m}.log", "a", encoding="utf-8") as fh:
            fh.write("\t".join([when.strftime("%Y-%m-%dT%H:%M:%SZ"), "skill", clean(name),
                                clean(project), clean(socket.gethostname())]) + "\n")
    except Exception:  # noqa: BLE001 — a tally must never break a turn
        pass


def typed_skill(line: str, skills: set[str]) -> str | None:
    """The skill a typed line invokes — `/paper-writing methods` — or None.

    Only a name that IS a skill: `/model`, `/tmp/x.png` and a sentence that
    happens to start with a slash are not one.
    """
    text = (line or "").lstrip()
    if not text.startswith("/"):
        return None
    name = text[1:].split(None, 1)[0] if len(text) > 1 else ""
    return name if name in skills else None
