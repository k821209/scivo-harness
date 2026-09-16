"""The interactive loop."""

from __future__ import annotations

import asyncio
import json
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
from .preflight import to_markdown
from .session import Session, scivo_tool_label

LOCAL_COMMANDS = {
    "/exit": "leave",
    "/quit": "leave",
    "/status": "reprint the session briefing",
    "/blocked": "what the guardrails stopped this session",
    "/cost": "spend so far",
    "/tools": "which scivo tools this profile loaded",
    "/help": "this list",
}


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

    def _on_assistant(self, message: AssistantMessage) -> None:
        for block in message.content:
            if isinstance(block, TextBlock):
                # Already printed as deltas unless partial streaming was absent.
                if block.text.strip() and block.text not in self._streamed:
                    print(block.text, end="", flush=True)
            elif isinstance(block, ThinkingBlock):
                continue
            elif isinstance(block, ToolUseBlock):
                if self._streamed or not self._streamed:
                    print()
                label = scivo_tool_label(block.name)
                detail = _preview(block.name, block.input or {})
                line = f"  · {label}" + (f"  {detail}" if detail else "")
                print(ui.dim(line), flush=True)
        self._streamed = ""

    def _on_result(self, message: ResultMessage) -> None:
        self.turns += message.num_turns
        if message.total_cost_usd:
            self.cost += message.total_cost_usd
        if message.is_error:
            print(ui.red(f"\n  ! {message.stop_reason or 'error'}: {message.result or ''}"))
        print(ui.dim(f"\n  ({message.num_turns} turns · ${message.total_cost_usd or 0:.3f} · "
                     f"${self.cost:.3f} session)\n"))

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
            print(ui.dim("\n  any other /name runs the skill of that name.\n"))
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

    # ------------------------------------------------------------- the loop

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

        async with ClaudeSDKClient(options=self.session.options) as client:
            while True:
                try:
                    line = (await asyncio.to_thread(input, ui.cyan("scivo› "))).strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not line:
                    continue
                try:
                    if self._local(line):
                        continue
                except EOFError:
                    break

                await client.query(line)
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
        print(ui.dim(f"session total ${self.cost:.4f}"))
