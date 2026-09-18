"""Asking before a tool runs — and saying something useful when we cannot ask.

The harness sets `allowed_tools` for the read-only surface, which needs no
approval, and left everything else to a permission prompt that did not exist.
Without a `can_use_tool` callback there is no channel to ask on, so the CLI
denies and the model is told only "you haven't granted it yet" — with no way for
anyone to grant it. Every write, every edit, silently unavailable.

Two handlers, because the two session shapes differ in kind: an interactive
session can ask a person, and a one-shot `scivo run` cannot ask anyone. The
second does not pretend otherwise — it denies with the flag that would have
allowed it, so the model relays an instruction instead of guessing at a cause.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from . import ui
from .interrupts import paused
from .session import scivo_tool_label

# Tools whose effects leave this machine or cannot be undone from here. They are
# never remembered as "always" — approving one is approving that one.
OUTWARD = ("youtube_", "publish_page", "update_publication", "add_passcode",
           "submit_remote_job", "kill_remote_job", "launch_local_job")


def describe(tool_name: str, payload: dict[str, Any]) -> str:
    label = scivo_tool_label(tool_name)
    if tool_name == "Bash":
        return f"{label}: {str(payload.get('command', ''))[:160]}"
    if tool_name in {"Write", "Edit", "NotebookEdit"}:
        return f"{label}: {payload.get('file_path', '')}"
    interesting = {k: v for k, v in payload.items()
                   if k in ("slug", "name", "analysis", "alias", "title", "doi", "section_key")}
    detail = json.dumps(interesting, ensure_ascii=False)[:160] if interesting else ""
    return f"{label}{(' ' + detail) if detail else ''}"


def is_outward(tool_name: str) -> bool:
    return any(marker in tool_name for marker in OUTWARD)


class Approvals:
    """Interactive approval, with an 'always' that is scoped to one tool name."""

    def __init__(self) -> None:
        self.always: set[str] = set()
        self.denied: list[str] = []
        # Set by `/scivo-control`. While the web page drives the session the
        # question is asked there: a thread blocked on the terminal's stdin
        # cannot be withdrawn when the answer arrives from the page, and would
        # then swallow the next line typed.
        self.remote: Any = None
        # One question at a time. A model that emits five update_section calls
        # in one turn gets five permission requests at once; answered
        # concurrently they were five prompts on screen and five threads
        # reading stdin, so which call a typed "n" landed on was a race — a
        # person reading the prompt for one section could deny another.
        self._one_at_a_time = asyncio.Lock()

    async def __call__(self, tool_name: str, payload: dict[str, Any], context: Any):
        # A guardrail hold arrives with the hook's reason. It is a question
        # about THIS call's content, so an earlier "always" for the tool does
        # not answer it.
        hold = getattr(context, "decision_reason", None)
        if tool_name in self.always and not hold:
            return PermissionResultAllow(updated_input=payload)
        async with self._one_at_a_time:
            # Checked again once it is this call's turn: "always" on the first
            # of a batch should settle the rest without asking.
            if tool_name in self.always and not hold:
                return PermissionResultAllow(updated_input=payload)
            # stdin goes back to line mode BEFORE the question is printed.
            # Pausing around the input() call alone left a window between the
            # printed question and the read, and an answer typed in that window
            # went to the Esc watcher instead — the prompt then sat there
            # ignoring the keyboard.
            terminal = self.remote is None or not self.remote.active
            with paused() if terminal else contextlib.nullcontext():
                if hold:
                    return await self._hold(tool_name, payload, hold)
                return await self._ask(tool_name, payload)

    async def _hold(self, tool_name: str, payload: dict[str, Any], reason: str):
        """A guardrail asked. The prompt used to read like every other one:
        "permission append_project_memory", Enter to allow. So the hold on
        hardware in project memory went through on a reflexive Enter, with no
        sign of what was held or why. Show both, and make only "y" a yes."""
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        print()
        print(ui.red(f"  held  {scivo_tool_label(tool_name)}"))
        for line in reason.splitlines():
            print(ui.dim(f"        {line}"))
        print(ui.dim("        ── the call ──"))
        for line in body.splitlines()[:30]:
            print(f"        {line}")
        if self.remote is not None and self.remote.active:
            print(ui.dim("        waiting for your answer on the scivo-control page…"))
            answer = await self.remote.ask_approval(scivo_tool_label(tool_name),
                                                    f"HELD: {reason}\n\n{body}", True)
            print(ui.dim(f"        {answer} (from the page)"))
        else:
            answer = (await asyncio.to_thread(input, ui.cyan("  allow anyway? y / [N] "))).strip().lower()
        if answer in {"y", "yes", "allow"}:
            return PermissionResultAllow(updated_input=payload)
        self.denied.append(tool_name)
        return PermissionResultDeny(
            message="The user did not approve this held call. " + reason
        )

    async def _ask(self, tool_name: str, payload: dict[str, Any]):

        outward = is_outward(tool_name)
        print()
        print(ui.yellow(f"  permission  {describe(tool_name, payload)}"))
        if outward:
            print(ui.dim("              this one reaches outside this machine"))

        if self.remote is not None and self.remote.active:
            print(ui.dim("              waiting for your answer on the scivo-control page…"))
            answer = await self.remote.ask_approval(scivo_tool_label(tool_name),
                                                    describe(tool_name, payload), outward)
            print(ui.dim(f"              {answer} (from the page)"))
        else:
            options = "[y]es / [n]o" if outward else "[y]es / [a]lways / [n]o"
            answer = (await asyncio.to_thread(input, ui.cyan(f"  {options}? "))).strip().lower()

        if answer in {"a", "always"}:
            # For a tool that reaches outside this machine "always" is not
            # remembered, but it is still a yes to this call — never a no.
            if not outward:
                self.always.add(tool_name)
            return PermissionResultAllow(updated_input=payload)
        # "allow" is what the page sends; y/yes/empty is the terminal. Missing the
        # first once made an approved call land in the deny branch below while
        # the page showed "allowed".
        if answer in {"y", "yes", "", "allow"}:
            return PermissionResultAllow(updated_input=payload)

        self.denied.append(tool_name)
        return PermissionResultDeny(
            message="The user declined this tool call. Do not retry it; say what you "
                    "would have done and let them decide."
        )


def outside_project(payload: dict[str, Any], root: str) -> str | None:
    """The file path this call targets, when it lies outside the project.

    A permission mode does not lift this: Claude Code scopes file writes to the
    working directory and whatever `--add-dir` adds. Saying "pass acceptEdits"
    to someone writing to /data sends them round a loop that cannot close.
    """
    path = payload.get("file_path") or payload.get("notebook_path")
    if not path:
        return None
    try:
        resolved = Path(str(path)).resolve()
        resolved.relative_to(Path(root).resolve())
    except ValueError:
        return str(resolved)
    except OSError:
        return None
    return None


class Explain:
    """Non-interactive: deny, but say what would have allowed it."""

    def __init__(self, mode: str, root: str | None = None) -> None:
        self.mode = mode
        self.root = root or os.getcwd()
        self.denied: list[str] = []

    async def __call__(self, tool_name: str, payload: dict[str, Any], context: Any):
        self.denied.append(tool_name)
        label = scivo_tool_label(tool_name)
        hold = getattr(context, "decision_reason", None)
        if hold:
            # A guardrail hold is about the content, not a missing mode; the
            # generic advice below would point at bypassPermissions.
            return PermissionResultDeny(message=f"Held and not approved (nobody to ask in `scivo run`). {hold}")
        elsewhere = outside_project(payload, self.root)

        if elsewhere:
            return PermissionResultDeny(
                message=(
                    f"Denied: `{label}` targets {elsewhere}, which is outside this "
                    f"project ({self.root}).\n"
                    "A permission mode does not lift that — file access is scoped to the "
                    "project directory. Tell the user to restart with "
                    f"`scivo --add-dir {Path(elsewhere).parent}`, or to work inside the "
                    "project. Do not try another tool to reach the same path."
                )
            )

        return PermissionResultDeny(
            message=(
                f"Denied: `{label}` needs approval and this is a one-shot `scivo run`, "
                "which has nobody to ask.\n"
                "Tell the user to re-run it in an interactive session (`scivo`), where "
                "they will be prompted, or to pass a permission mode that does not ask — "
                "`--permission-mode acceptEdits` for file edits, `bypassPermissions` for "
                "everything. Do not work around it with another tool."
            )
        )
