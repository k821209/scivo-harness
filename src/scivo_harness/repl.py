"""The interactive loop."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import sys
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from . import ui
from .control import Control
from .preflight import to_markdown
from .failures import explain
from .prompt_line import Line
from .permissions import is_outward
from .session import Session, scivo_tool_label
from .toolsets import PREFIX
from .skills import discover

LOCAL_COMMANDS = {
    "/exit": "leave",
    "/quit": "leave",
    "/status": "reprint the session briefing",
    "/blocked": "what the guardrails stopped this session",
    "/cost": "spend so far",
    "/session": "this session's id, to resume it later",
    "/permissions": "show or change: default · acceptEdits · auto · plan · always <tool> · always -<tool>",
    "/dangerously-skip-permissions": "stop asking for anything (`off` to ask again); guardrails still apply",
    "/tools": "which scivo tools this profile loaded",
    "/scivo-control": "drive this session from the scivo web page (`off` to stop)",
    "/help": "this list",
}

ANSI = re.compile(r"\x1b\[[0-9;]*m")

MODE_TAGS = {"default": "", "acceptEdits": "edits", "auto": "auto", "plan": "plan",
             "bypassPermissions": "skip", "dontAsk": "dontAsk"}
MODE_ALIASES = {"default": "default", "ask": "default", "acceptedits": "acceptEdits",
                "edits": "acceptEdits", "auto": "auto", "plan": "plan",
                "bypass": "bypassPermissions", "bypasspermissions": "bypassPermissions",
                "skip": "bypassPermissions"}


def _preview(tool: str, payload: dict[str, Any]) -> str:
    """One line describing a tool call, without dumping its arguments."""
    if tool == "Bash":
        return str(payload.get("command", ""))[:110]
    if tool in {"Read", "Write", "Edit"}:
        return str(payload.get("file_path", ""))
    if tool == "Task":
        return str(payload.get("description", ""))
    interesting = [k for k in ("slug", "name", "analysis", "alias", "doi", "query", "title") if k in payload]
    if interesting:
        return " ".join(f"{k}={json.dumps(payload[k], ensure_ascii=False)[:40]}" for k in interesting)
    return ""


class Repl:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.cost = 0.0
        self.turns = 0
        self._streamed = ""
        self.skills = discover(session.config.root)
        self.line = Line(session.config.root, LOCAL_COMMANDS, self.skills)
        self.control: Control | None = None
        self.session_id: str | None = session.options.resume
        self.mode: str = session.options.permission_mode or "default"

    # ------------------------------------------------------------ rendering

    def _on_stream(self, event: StreamEvent) -> None:
        raw = event.event or {}
        if raw.get("type") != "content_block_delta":
            return
        delta = raw.get("delta") or {}
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            print(text, end="", flush=True)
            self._streamed += text
            if self.control:
                self.control.delta(text)

    def _on_assistant(self, message: AssistantMessage) -> None:
        for block in message.content:
            if isinstance(block, TextBlock):
                # Already printed as deltas unless partial streaming was absent.
                if block.text.strip() and block.text not in self._streamed:
                    print(block.text, end="", flush=True)
                    if self.control:
                        self.control.delta(block.text)
            elif isinstance(block, ThinkingBlock):
                continue
            elif isinstance(block, ToolUseBlock):
                if self._streamed or not self._streamed:
                    print()
                label = scivo_tool_label(block.name)
                detail = _preview(block.name, block.input or {})
                line = f"  · {label}" + (f"  {detail}" if detail else "")
                print(ui.dim(line), flush=True)
                if self.control:
                    self.control.tool(label, detail)
        self._streamed = ""
        if self.control:
            self.control.end_assistant()

    def _on_result(self, message: ResultMessage) -> None:
        self.session_id = message.session_id or self.session_id
        self.turns += message.num_turns
        if message.total_cost_usd:
            self.cost += message.total_cost_usd
        if message.is_error:
            print(ui.red(f"\n  ! {message.stop_reason or 'error'}: {message.result or ''}"))
        print(ui.dim(f"\n  ({message.num_turns} turns · ${message.total_cost_usd or 0:.3f} · "
                     f"${self.cost:.3f} session)\n"))
        if self.control:
            self.control.result(message.num_turns, message.total_cost_usd or 0.0, self.cost,
                                error=(message.stop_reason or "error") if message.is_error else None)

    # -------------------------------------------------------- local commands

    def _local(self, line: str) -> bool:
        """True if the line was handled here and must not reach the model."""
        command = line.split()[0]
        if command not in LOCAL_COMMANDS:
            return False
        if command in {"/exit", "/quit"}:
            raise EOFError
        if command == "/help":
            print()
            for name, description in LOCAL_COMMANDS.items():
                print(f"  {ui.cyan(name):<22} {description}")
            print(ui.dim(f"\n  and {len(self.skills)} skills — press / to list them"
                         + ("" if self.line.rich else " (see `scivo status`)") + "\n"))
        elif command == "/status":
            print("\n" + to_markdown(self.session.briefing) + "\n")
        elif command == "/blocked":
            rails = self.session.rails
            print()
            if rails.blocked:
                for item in rails.blocked:
                    print(ui.yellow(f"  blocked: {item}"))
            else:
                print(ui.dim("  nothing blocked this session."))
            print(ui.dim(f"  analysis-shaped shell commands: {rails.adhoc_runs}\n"))
        elif command == "/session":
            if self.session_id:
                print(f"\n  {self.session_id}")
                print(ui.dim(f"  resume later: scivo resume {self.session_id[:8]}\n"))
            else:
                print(ui.dim("\n  no id yet — it is assigned with the first reply\n"))
        elif command == "/cost":
            print(ui.dim(f"\n  ${self.cost:.4f} over {self.turns} turns\n"))
        elif command == "/tools":
            plan = self.session.plan
            print(f"\n  {plan.summary()}")
            if plan.dropped:
                print(ui.dim(f"  dropped: {', '.join(plan.dropped[:12])}"
                             + (" …" if len(plan.dropped) > 12 else "")))
            print()
        return True

    # ------------------------------------------------------- scivo-control

    async def _control(self, argument: str) -> None:
        if argument == "off":
            if self.control and self.control.active:
                await self.control.stop()
                print(ui.dim("\n  scivo-control off — the page is unpublished until you turn it on again\n"))
            else:
                print(ui.dim("\n  scivo-control is not on\n"))
            return

        if self.control and self.control.active:
            self._print_link()
            return

        briefing = self.session.briefing
        self.control = Control(self.session.config, briefing.project_name,
                               briefing.project_id, self.session.model)
        print(ui.dim("\n  publishing the control page…"))
        try:
            await self.control.start()
        except Exception as exc:  # noqa: BLE001
            print(ui.red(f"  scivo-control failed to start: {exc}\n"))
            self.control = None
            return
        commands = [{"name": name, "help": text} for name, text in LOCAL_COMMANDS.items()]
        commands += [{"name": f"/{skill.name}", "help": skill.summary} for skill in self.skills]
        await self.control.put_commands(commands)
        if self.session.approver is not None:
            self.session.approver.remote = self.control
        self._print_link()

    def _print_link(self) -> None:
        link = self.control.link
        print()
        print(ui.green("  scivo-control on"))
        print(f"  open      {ui.bold(link.url)}")
        print(f"  passcode  {ui.bold(link.passcode)}   {ui.dim('(yours only — it acts on this machine)')}")
        print(ui.dim("  The page drives this session: messages, approvals and Stop."))
        print(ui.dim("  Typing here still works. `/scivo-control off` unpublishes the page.\n"))

    async def _next_input(self) -> tuple[str, str]:
        """The next line, from the terminal or the web page, whichever comes first."""
        tag = MODE_TAGS.get(self.mode, self.mode)
        if tag:
            colour = ui.red if self.mode == "bypassPermissions" else ui.yellow
            prompt = ui.cyan("scivo") + colour(f"[{tag}]") + ui.cyan("› ")
        else:
            prompt = ui.cyan("scivo› ")
        plain = f"scivo[{tag}]> " if tag else "scivo> "
        if not (self.control and self.control.active):
            return await self.line.ask(prompt, plain), "terminal"

        web = asyncio.create_task(self.control.messages.get())
        waiting = {web}
        terminal = None
        # A plain stdin read runs in a thread that cannot be withdrawn, so it
        # would swallow the next typed line after a web message won. Only the
        # prompt_toolkit prompt can be cancelled cleanly.
        if self.line.rich:
            terminal = asyncio.create_task(self.line.ask(prompt, plain))
            waiting.add(terminal)
        done, pending = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        if terminal is not None and terminal in done:
            if web in done:  # both at once: keep the web message for next time
                await self.control.messages.put(web.result())
            return terminal.result(), "terminal"
        text = web.result()
        print(f"{prompt}{text}  {ui.dim('[web]')}")
        return text, "web"

    # ---------------------------------------------------------- permissions

    def _say(self, text: str) -> None:
        print(text)
        if self.control and self.control.active:
            self.control.note(f"```\n{ANSI.sub('', text).strip()}\n```")

    async def _set_mode(self, client: ClaudeSDKClient, mode: str) -> None:
        try:
            await client.set_permission_mode(mode)
        except Exception as exc:  # noqa: BLE001
            self._say(ui.red(f"\n  could not switch to {mode}: {exc}\n"))
            return
        self.mode = mode
        notes = {
            "default": "asks before each tool that needs approval",
            "acceptEdits": "file edits run without asking; other tools still ask",
            "auto": "a classifier approves or denies each call; the risky ones are refused",
            "plan": "reads and plans only — nothing that changes anything runs",
            "bypassPermissions": "nothing asks, including tools that reach outside this machine "
                                 "(uploads, publishing, remote jobs). Guardrails still block "
                                 "raw ssh jobs and remote pkill -f. `/dangerously-skip-permissions off` to stop.",
        }
        colour = ui.red if mode == "bypassPermissions" else ui.green
        self._say(colour(f"\n  permissions: {mode}") + ui.dim(f" — {notes.get(mode, '')}\n"))

    def _tool(self, token: str) -> str:
        if token.startswith("mcp__"):
            return token
        if token in self.session.plan.kept:
            return PREFIX + token
        return token

    async def _permissions(self, client: ClaudeSDKClient, command: str, argument: str) -> None:
        approver = self.session.approver
        if command == "/dangerously-skip-permissions":
            await self._set_mode(client, "default" if argument == "off" else "bypassPermissions")
            return

        words = argument.split()
        if not words:
            always = sorted(scivo_tool_label(name) for name in (approver.always if approver else set()))
            self._say("\n  mode    " + self.mode
                      + "\n  always  " + (", ".join(always) if always else "(none)")
                      + ui.dim("\n\n  /permissions default | acceptEdits | auto | plan"
                               "\n  /permissions always <tool>     ·   /permissions always -<tool>"
                               "\n  /dangerously-skip-permissions  ·   /dangerously-skip-permissions off\n"))
            return

        if words[0] == "always":
            if approver is None or len(words) < 2:
                self._say(ui.dim("\n  usage: /permissions always <tool>   (or -<tool> to remove)\n"))
                return
            for token in words[1:]:
                remove = token.startswith("-")
                name = self._tool(token.lstrip("-"))
                label = scivo_tool_label(name)
                if remove:
                    approver.always.discard(name)
                    self._say(ui.dim(f"  {label}: asks again"))
                elif is_outward(name):
                    self._say(ui.yellow(f"  {label} reaches outside this machine and is approved one call "
                                        "at a time. /dangerously-skip-permissions stops all asking, if that is what you mean."))
                else:
                    approver.always.add(name)
                    self._say(ui.green(f"  {label}: always allowed this session"))
            return

        mode = MODE_ALIASES.get(words[0].lower())
        if mode is None:
            self._say(ui.red(f"\n  unknown mode {words[0]!r}") + ui.dim(" — default, acceptEdits, auto, plan\n"))
            return
        await self._set_mode(client, mode)

    # ------------------------------------------------------------- the loop

    def _run_local(self, line: str) -> bool:
        """Run a local command, mirroring its output to the page when it is on."""
        if line.split()[0] not in LOCAL_COMMANDS:
            return False
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                self._local(line)
        finally:
            # Written back only once the redirect has ended. Inside it, the echo
            # went into the same buffer and the terminal showed nothing at all.
            captured = buffer.getvalue()
            sys.stdout.write(captured)
            sys.stdout.flush()
        if self.control and self.control.active and captured.strip():
            plain = ANSI.sub("", captured).strip("\n")
            is_markdown = line.split()[0] == "/status"
            self.control.note(plain if is_markdown else f"```\n{plain}\n```")
        return True

    async def _turn(self, client: ClaudeSDKClient, line: str) -> None:
        await client.query(line)
        watcher = None
        if self.control and self.control.active:
            self.control.interrupt_requested.clear()

            async def watch() -> None:
                await self.control.interrupt_requested.wait()
                await client.interrupt()
                print(ui.yellow("\n  interrupted from the page"))
                self.control.status("interrupted", level="warn")

            watcher = asyncio.create_task(watch())
        try:
            async for message in client.receive_response():
                if isinstance(message, StreamEvent):
                    self._on_stream(message)
                elif isinstance(message, AssistantMessage):
                    self._on_assistant(message)
                elif isinstance(message, ResultMessage):
                    self._on_result(message)
        except KeyboardInterrupt:
            await client.interrupt()
            print(ui.yellow("\n  interrupted\n"))
            if self.control:
                self.control.status("interrupted from the terminal", level="warn")
        except Exception as exc:  # noqa: BLE001
            # One failed turn should not end the session: the briefing
            # and the conversation so far are worth more than the turn.
            message = explain(exc)
            text = message or f"{type(exc).__name__}: {exc}"
            print(ui.red(f"\n  {text}\n"))
            if self.control:
                self.control.status(text, level="error")
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    async def run(self) -> None:
        briefing = self.session.briefing
        label = self.session.model
        if self.session.provider.is_local:
            label += f" @ {self.session.provider.name}"
        print(ui.banner(briefing.project_name, briefing.project_id,
                        self.session.plan.summary(), label))
        for line in ui.attention(briefing):
            print(line)
        print()

        try:
            async with ClaudeSDKClient(options=self.session.options) as client:
                while True:
                    try:
                        line, via = await self._next_input()
                        line = line.strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        break
                    if not line:
                        continue

                    command, _, argument = line.partition(" ")
                    if command == "/scivo-control":
                        await self._control(argument.strip())
                        continue
                    if command in {"/permissions", "/dangerously-skip-permissions"}:
                        if self.control and self.control.active:
                            self.control.user(line, via)
                        await self._permissions(client, command, argument.strip())
                        continue
                    if self.control and self.control.active:
                        self.control.user(line, via)
                    try:
                        if self._run_local(line):
                            continue
                    except EOFError:
                        break

                    await self._turn(client, line)
        finally:
            if self.control and self.control.active:
                await self.control.stop()
        print(ui.dim(f"session total ${self.cost:.4f}"))
        if self.session_id:
            print(ui.dim(f"resume: scivo resume {self.session_id[:8]}   (or scivo -c)"))
