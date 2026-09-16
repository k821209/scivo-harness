"""Terminal rendering. Deliberately small: a line protocol, not a TUI."""

from __future__ import annotations

import os
import sys

_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return text if _NO_COLOR else f"\033[{code}m{text}\033[0m"


def dim(text: str) -> str:
    return _c("2", text)


def bold(text: str) -> str:
    return _c("1", text)


def cyan(text: str) -> str:
    return _c("36", text)


def green(text: str) -> str:
    return _c("32", text)


def yellow(text: str) -> str:
    return _c("33", text)


def red(text: str) -> str:
    return _c("31", text)


def banner(name: str, project_id: str, plan_summary: str, model: str) -> str:
    return "\n".join([
        bold(f"scivo · {name}"),
        dim(f"  project {project_id} · {model} · {plan_summary}"),
        dim("  /help for commands, /exit to leave"),
    ])


def attention(briefing) -> list[str]:
    """The lines a researcher needs before typing anything."""
    lines = []
    for paper in briefing.attention:
        bits = []
        if paper.open_comments:
            bits.append(f"{paper.open_comments} open comment(s)")
        if paper.triage.get("ai_open"):
            bits.append(f"{paper.triage['ai_open']} AI finding(s)")
        if paper.triage.get("rejected_without_rationale"):
            bits.append(f"{paper.triage['rejected_without_rationale']} unrebutted rejection(s)")
        if paper.requirement_violations:
            bits.append(f"{len(paper.requirement_violations)} requirement violation(s)")
        if paper.findings:
            bits.append(f"{len(paper.findings)} citation finding(s)")
        lines.append(yellow(f"  ! {paper.slug}: " + ", ".join(bits)))
    for error in briefing.errors:
        lines.append(red(f"  ! preflight: {error}"))
    return lines
