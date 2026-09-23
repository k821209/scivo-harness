"""The interactive loop."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import sys
import time
from typing import Any

from claude_agent_sdk import (
    ConversationResetMessage,
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
)

from . import guardrails, ui
from .control import Control
from .preflight import to_markdown
from .failures import explain
from .interrupts import KeyWatcher
from .prompt_line import Line
from .permissions import is_outward
from .session import Session, scivo_tool_label
from .toolsets import PREFIX

CLAUDE_MODELS = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5",
                 "haiku": "claude-haiku-4-5", "fable": "claude-fable-5-1"}
from .skills import discover

# Handled by Claude Code itself, not here: they go through as a prompt. Listed
# so they complete and appear in /help, because a command that works but cannot
# be discovered may as well not exist.
PASSTHROUGH = {"/compact", "/clear"}

LOCAL_COMMANDS = {
    "/exit": "leave",
    "/quit": "leave",
    "/status": "reprint the session briefing",
    "/blocked": "what the guardrails stopped this session",
    "/cost": "spend so far",
    "/session": "this session's id, to resume it later",
    "/model": "switch model: opus · sonnet · haiku · fable, or a provider such as local",
    "/permissions": "show or change: default · acceptEdits · auto · plan · always <tool> · always -<tool>",
    "/dangerously-skip-permissions": "stop asking for anything (`off` to ask again); guardrails still apply",
    "/tools": "which scivo tools this profile loaded",
    "/context": "how much of the context window is in use, and what fills it",
    "/compact": "summarise the conversation so far and carry on with a shorter one",
    "/clear": "start a fresh conversation — the way out when the old one no longer fits",
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


def _result_reason(block) -> str:
    """One-line summary of why a call failed or was blocked.

    Reading `#76 failed after 0.0s` on its own left the user wondering whether
    the call was still running; the reason line makes the state read at a
    glance: an aspect_ratio validation error is a real failure, a Blocked
    line is a nudge to try another way.
    """
    content = getattr(block, "content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    head = str(content).lstrip().replace("\n", " ")
    # trim wrappers we already convey by colour
    for prefix in ("<tool_use_error>", "Error executing tool ", "Blocked: ", "Held ",
                   "The user declined this tool call. ", "The user did not approve this held call. "):
        if head.startswith(prefix):
            head = head[len(prefix):]
    head = head.strip("\"' \t")
    return head[:80] + ("…" if len(head) > 80 else "")


def _result_state(block) -> str:
    """Classify a tool_result: `blocked` (a soft refusal we can rephrase),
    `failed` (something really went wrong), or `ok`.

    Claude Code's own tool guards return `<tool_use_error>Blocked: …` on things
    like `sleep N` chained to a command, and our permission callback denies
    with a message starting `Blocked:` or `Held`. Painting those red as
    "failed" reads as a fault; they are a nudge to try another approach.
    """
    if not bool(getattr(block, "is_error", False)):
        return "ok"
    content = getattr(block, "content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    head = str(content).lstrip()[:120]
    if head.startswith("<tool_use_error>") or head.startswith("Blocked:") or head.startswith("Held"):
        return "blocked"
    if "declined this tool call" in head or "did not approve this held call" in head:
        return "blocked"
    return "failed"


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
        self._compacting = False
        self._typed_ahead = ""
        self._stopping = False
        self.tool_calls = 0
        self._tool_started: dict[str, tuple[int, float]] = {}
        self._activity_shown = False
        self._last_output = time.monotonic()
        ui.clear_activity = self._clear_activity

    # ------------------------------------------------------------ rendering

    def _on_stream(self, event: StreamEvent) -> None:
        self._mark_output()
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

    # ------------------------------------------------------------- waiting

    def _mark_output(self) -> None:
        """Something is about to print: wipe the waiting line, restart the clock."""
        self._clear_activity()
        self._last_output = time.monotonic()

    def _clear_activity(self) -> None:
        if getattr(self, "_activity_shown", False):
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self._activity_shown = False

    async def _heartbeat(self) -> None:
        """A turn with nothing to show looks exactly like a hung one.

        Latency before the first token, a slow tool, a remote command that
        takes minutes — all printed nothing at all, and the session read as
        frozen. This draws the seconds on one line, which the next real output
        wipes.
        """
        frames = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
        started = time.monotonic()
        frame = 0
        try:
            while True:
                await asyncio.sleep(0.4)
                if time.monotonic() - self._last_output < 2.0 or self._stopping:
                    continue
                elapsed = int(time.monotonic() - started)
                shown = f"{elapsed}s" if elapsed < 60 else f"{elapsed // 60}m{elapsed % 60:02d}s"
                frame = (frame + 1) % len(frames)
                tail = "  \u00b7  esc to stop" if elapsed >= 10 else ""
                sys.stdout.write("\r\033[K  " + ui.dim(f"{frames[frame]} working {shown}{tail}"))
                sys.stdout.flush()
                self._activity_shown = True
        except asyncio.CancelledError:
            self._clear_activity()
            raise

    def _on_assistant(self, message: AssistantMessage) -> None:
        self._mark_output()
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
                print()
                label = scivo_tool_label(block.name)
                detail = _preview(block.name, block.input or {})
                # Numbered and clocked: four identical `ssh …` lines in a row
                # gave no way to tell a retry from a call still running.
                self.tool_calls += 1
                self._tool_started[block.id] = (self.tool_calls, time.monotonic())
                stamp = time.strftime("%H:%M:%S")
                line = f"  #{self.tool_calls} {stamp}  {label}" + (f"  {detail}" if detail else "")
                print(ui.dim(line), flush=True)
                if self.control:
                    self.control.tool(f"#{self.tool_calls} {stamp} {label}", detail)
        self._streamed = ""
        if self.control:
            self.control.end_assistant()

    def _on_tool_result(self, message: Any) -> None:
        """Close the line its call opened, with how long it took.

        Without this a long `ssh` and a stuck one look the same, and a model
        that reissues a call looks like one call being slow.
        """
        self._mark_output()
        content = getattr(message, "content", None)
        for block in content if isinstance(content, list) else []:
            identifier = getattr(block, "tool_use_id", None)
            if identifier is None or identifier not in self._tool_started:
                continue
            number, started = self._tool_started.pop(identifier)
            seconds = time.monotonic() - started
            took = f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
            state = _result_state(block)
            mark = {"blocked": "blocked after", "failed": "failed after"}.get(state, "done in")
            reason = _result_reason(block) if state != "ok" else ""
            line = f"  #{number} {mark} {took}" + (f"  {reason}" if reason else "")
            paint = {"blocked": ui.yellow, "failed": ui.red}.get(state, ui.dim)
            print(paint(line), flush=True)
            if self.control:
                self.control.tool(f"#{number}", f"{mark} {took}" + (f" — {reason}" if reason else ""))

    def _on_system(self, message: SystemMessage) -> None:
        """Compaction is the one background step long enough to look like a hang."""
        data = message.data or {}
        if message.subtype == "status" and data.get("status") == "compacting":
            if not self._compacting:
                self._compacting = True
                self._say(ui.dim("\n  compacting the conversation…"))
            return
        if message.subtype == "status" and data.get("compact_result"):
            self._compacting = False
            outcome = str(data["compact_result"])
            if outcome == "success":
                self._say(ui.dim(f"  compaction {outcome}"))
                return
            # Compacting sends the whole conversation to the model, so a
            # conversation already over the window cannot be compacted out of
            # it. /clear is the way out, and it keeps the session and its id.
            self._say(ui.red(f"  compaction {outcome}")
                      + ui.dim("\n  A conversation that no longer fits cannot be summarised either —"
                               "\n  summarising sends all of it to the model. `/clear` starts a fresh"
                               "\n  conversation; this one stays in `scivo sessions`.\n"))
            return
        if message.subtype == "compact_boundary":
            meta = data.get("compact_metadata") or {}
            before = meta.get("pre_tokens")
            trigger = meta.get("trigger", "")
            detail = f" — {before:,} tokens summarised" if isinstance(before, int) else ""
            self._say(ui.dim(f"  conversation compacted ({trigger}){detail}\n"))

    def _on_retry(self, data: dict) -> None:
        """Claude Code retries a failing API call quietly. Say so, or it looks hung."""
        import json as _json

        error = str(data.get("error") or "")
        try:  # llama-server and the API both wrap the useful part in JSON
            inner = _json.loads(error[error.index("{"):])
            error = str((inner.get("error") or {}).get("message") or error)
        except (ValueError, AttributeError):
            pass
        error = " ".join(error.split())
        hint = ""
        if "System message must be at the beginning" in error:
            hint = "  — this server needs the shim; restart the session or /model to it again"
        delay = (data.get("retry_delay_ms") or 0) / 1000
        attempt = f"attempt {data.get('attempt')}/{data.get('max_retries')}, retrying in {delay:.0f}s"
        if data.get("error_status") is None and error in {"", "unknown"}:
            # No HTTP status and no body: the connection itself failed.
            where = self.session.provider.base_url or "the Anthropic API"
            line = f"\n  cannot reach {where} ({attempt}) — is the server running?"
        else:
            line = f"\n  api error {data.get('error_status')} ({attempt}): {error[-160:]}{hint}"
        print(ui.yellow(line), flush=True)
        if self.control and self.control.active:
            self.control.status(line.strip(), level="warn")

    def _on_result(self, message: ResultMessage) -> None:
        self.session_id = message.session_id or self.session_id
        self.turns += message.num_turns
        # Claude Code prices every turn from its own table, including models it
        # does not know: a Qwen turn on a local server came back as $0.334.
        # Nothing was billed, so it is neither shown nor added to the total.
        local = self.session.provider.is_local
        turn_cost = 0.0 if local else (message.total_cost_usd or 0.0)
        self.cost += turn_cost
        if message.is_error and not self._stopping:
            # A turn stopped on purpose comes back as an error with no text.
            # Reporting "! error:" for a stop the person asked for reads as a
            # fault in the harness.
            print(ui.red(f"\n  ! {message.stop_reason or 'error'}: {message.result or ''}"))
        price = "local model" if local else f"${turn_cost:.3f}"
        print(ui.dim(f"\n  ({message.num_turns} turns · {price} · ${self.cost:.3f} session)\n"))
        if self.control:
            self.control.result(message.num_turns, turn_cost, self.cost,
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
            print(ui.dim("  Esc                    stop the turn that is running"))
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
            if self.session.local_tools:
                print(ui.dim(f"  local kit: {self.session.local_tools} of them are loaded — a local "
                             "model reads every schema on every request."))
                print(ui.dim("  `scivo --profile video` (or deck, analysis) loads that domain too."))
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
        self._post_recap_to_page()
        if self.session.approver is not None:
            self.session.approver.remote = self.control
        self._print_link()

    def _post_recap_to_page(self) -> None:
        """Put the conversation so far on the page it just opened.

        The page starts empty because it starts when you type the command, and
        everything before that — including the recap `-c` printed — went to the
        terminal only. Opening the page mid-conversation showed nothing and
        read like a different session.
        """
        from . import sessions

        identifier = self.session_id or self.session.resumed
        if not (identifier and self.control):
            return
        turns = sessions.recap(identifier, exchanges=3)
        if not turns:
            return
        lines = [f"**Earlier in this session** ({identifier[:8]})", ""]
        for role, text in turns:
            body = " ".join(text.split())
            if len(body) > 600:
                body = body[:600] + " …"
            lines.append(f"**{'you' if role == 'user' else 'scivo'}** — {body}")
            lines.append("")
        self.control.note("\n".join(lines))

    def _print_link(self) -> None:
        link = self.control.link
        print()
        print(ui.green("  scivo-control on"))
        print(f"  open      {ui.bold(link.url)}")
        print(ui.dim("            open it from the dashboard's dock, or this link while signed in"))
        print(ui.dim("            it opens for you and nobody else — no code to type or forward"))
        print(ui.dim("  The page drives this session: messages, approvals and Stop."))
        print(ui.dim("  Typing here still works. `/scivo-control off` unpublishes the page.\n"))

    async def _next_input(self) -> tuple[str, str]:
        """The next line, from the terminal or the web page, whichever comes first.

        The prompt is plain ASCII on purpose. It used to end in "›", which
        Unicode calls ambiguous-width: prompt_toolkit counts it as one column
        and a terminal configured for CJK draws it as two. Every redraw was
        then one column out, so the cursor sat on the character after the
        slash and ate it — "/model" showed as "om" with a block over it.
        """
        tag = MODE_TAGS.get(self.mode, self.mode)
        if tag:
            colour = ui.red if self.mode == "bypassPermissions" else ui.yellow
            prompt = ui.cyan("scivo") + colour(f"[{tag}]") + ui.cyan("> ")
        else:
            prompt = ui.cyan("scivo> ")
        plain = f"scivo[{tag}]> " if tag else "scivo> "
        typed, self._typed_ahead = self._typed_ahead, ""
        if not (self.control and self.control.active):
            return await self.line.ask(prompt, plain, default=typed), "terminal"

        web = asyncio.create_task(self.control.messages.get())
        waiting = {web}
        terminal = None
        # A plain stdin read runs in a thread that cannot be withdrawn, so it
        # would swallow the next typed line after a web message won. Only the
        # prompt_toolkit prompt can be cancelled cleanly.
        if self.line.rich:
            terminal = asyncio.create_task(self.line.ask(prompt, plain, default=typed))
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

    # ----------------------------------------------------------------- model

    async def _model(self, argument: str) -> None:
        from .providers import load_all
        from .session import DEFAULT_EFFORT, DEFAULT_MODEL

        provider = self.session.provider
        providers = load_all()
        if not argument:
            self._say(f"\n  model     {self.session.model}"
                      + f"\n  provider  {provider.name}" + (f" → {provider.base_url}" if provider.base_url else "")
                      + ui.dim("\n\n  Claude, keeps the conversation:  /model " + " | ".join(CLAUDE_MODELS)
                               + "\n  another endpoint, new conversation: /model "
                               + " | ".join(n for n in providers if n != "anthropic")
                               + ("" if len(providers) > 1 else "(none configured — scivo providers --init)")
                               + "\n  to make it stick for this project: scivo providers use <name>\n"))
            return

        wanted = argument.split()[0]
        claude_model = CLAUDE_MODELS.get(wanted.lower()) or (wanted if wanted.startswith("claude-") else None)

        if claude_model and not provider.is_local:
            try:
                await self.client.set_model(claude_model)
            except Exception as exc:  # noqa: BLE001
                self._say(ui.red(f"\n  could not switch to {claude_model}: {exc}\n"))
                return
            self.session.model = claude_model
            if self.control:
                self.control.model = claude_model
            self._say(ui.green(f"\n  model: {claude_model}") + ui.dim(" — same conversation continues\n"))
            return

        target_name = "anthropic" if claude_model else wanted
        if target_name not in providers:
            self._say(ui.red(f"\n  {wanted!r} is neither a Claude model nor a provider")
                      + ui.dim(" — /model shows the choices\n"))
            return
        target = providers[target_name]
        model = claude_model or target.model or DEFAULT_MODEL
        if target_name == provider.name and model == self.session.model:
            self._say(ui.dim(f"\n  already on {model}\n"))
            return

        # The endpoint lives in the Claude Code process's environment, so
        # changing it means a new process — and a new conversation. Carrying the
        # old one over is not attempted: its history holds Claude's thinking
        # blocks, which a local server is not known to accept.
        from .providers import ProviderError, prepare

        if target.is_local:
            self._say(ui.dim(f"\n  checking {target_name}…"))
        try:
            endpoint = await asyncio.to_thread(prepare, target)
        except ProviderError as exc:
            self._say(ui.red(f"  {exc}") + ui.dim(f"\n  still on {provider.name} ({self.session.model})\n"))
            return
        options = self.session.options_for(
            target,
            env=endpoint.env,
            model=model,
            effort=(self.session.options.effort or DEFAULT_EFFORT) if target.supports_effort else None,
            resume=None,
            continue_conversation=False,
        )
        if endpoint.note:
            self._say(ui.dim(f"  · {endpoint.note}"))
        self._say(ui.dim(f"  switching to {target_name} ({model}) — this starts a new conversation…"))
        await self.client.disconnect()
        self.client = ClaudeSDKClient(options=options)
        try:
            await self.client.connect()
        except Exception as exc:  # noqa: BLE001
            endpoint.close()
            self._say(ui.red(f"  could not start on {target_name}: {exc}"))
            self.client = ClaudeSDKClient(options=self.session.options)
            await self.client.connect()
            self._say(ui.dim(f"  back on {provider.name} ({self.session.model}), in a new conversation\n"))
            return
        if self.mode != (options.permission_mode or "default"):
            await self.client.set_permission_mode(self.mode)
        if self.session.endpoint is not None:
            self.session.endpoint.close()
        self.session.endpoint = endpoint
        self.session.options = options
        self.session.provider = target
        self.session.model = model
        self.session_id = None
        if self.control:
            self.control.model = model
        hint = ""
        self._say(ui.green(f"  now on {target_name} · {model}") + ui.dim(f"\n{hint}\n" if hint else "\n"))

    # ---------------------------------------------------------- permissions

    def _say(self, text: str) -> None:
        self._mark_output()
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
        name = line.split()[0]
        if name in PASSTHROUGH or name not in LOCAL_COMMANDS:
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

    async def _context(self, client: ClaudeSDKClient, brief: bool) -> None:
        """Context usage — a percentage after each turn, the breakdown on demand.

        A local model's window is small enough to hit in an afternoon, and the
        first sign of it used to be every turn failing at once.
        """
        try:
            # Bounded: an endpoint that never answers this must not hold the
            # session at the end of an otherwise finished turn.
            usage = await asyncio.wait_for(client.get_context_usage(), timeout=10)
        except Exception:  # noqa: BLE001 - never let a display fail a turn
            if not brief:
                self._say(ui.dim("\n  context usage is not available from this endpoint\n"))
            return
        total, limit = usage.get("totalTokens"), usage.get("maxTokens")
        percent = usage.get("percentage")
        if not isinstance(total, int) or not isinstance(limit, int) or not limit:
            return
        percent = int(percent if isinstance(percent, (int, float)) else total * 100 / limit)
        threshold = usage.get("autoCompactThreshold")
        line = f"  context {percent}%  ({total:,} of {limit:,} tokens)"
        if brief:
            near = isinstance(threshold, int) and total >= threshold * 0.8
            self._say((ui.yellow(line) if near else ui.dim(line))
                      + (ui.dim("  · /compact before it fills") if near else ""))
            return
        self._say("\n" + ui.cyan(line))
        if isinstance(threshold, int):
            self._say(ui.dim(f"  auto-compacts at {threshold:,}"
                             + ("" if usage.get("isAutoCompactEnabled") else " — but auto-compact is off")))
        for category in usage.get("categories", []):
            name, tokens = category.get("name"), category.get("tokens")
            if isinstance(tokens, int) and tokens:
                self._say(ui.dim(f"    {str(name):<20} {tokens:>8,}"))
        self._say("")

    async def _turn(self, client: ClaudeSDKClient, line: str) -> None:
        self.session.rails.new_turn()
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
        self._stopping = False
        if self.control and self.control.active:
            self.control.waiting = True
        self._last_output = time.monotonic()
        beat = asyncio.create_task(self._heartbeat())

        def stop_now() -> None:
            self._stopping = True
            self._say(ui.yellow("\n  stopping…"))
            asyncio.create_task(client.interrupt())

        try:
            with KeyWatcher(stop_now) as keys:
                async for message in client.receive_response():
                    if isinstance(message, StreamEvent):
                        self._on_stream(message)
                    elif isinstance(message, SystemMessage) and message.subtype == "api_retry":
                        self._on_retry(message.data or {})
                    elif isinstance(message, ConversationResetMessage):
                        self._say(ui.green("\n  conversation cleared")
                                  + ui.dim(" — a new conversation, with its own id. `/session` shows it;"
                                           "\n  the old one is still in `scivo sessions`.\n"))
                    elif isinstance(message, SystemMessage):
                        self._on_system(message)
                    elif isinstance(message, AssistantMessage):
                        self._on_assistant(message)
                    elif isinstance(message, UserMessage):
                        self._on_tool_result(message)
                    elif isinstance(message, ResultMessage):
                        self._on_result(message)
                    if self.session.rails.looping and not self._stopping:
                        # Denying the repeat did not stop it either. End the
                        # turn rather than let it spend the context on a loop.
                        self._stopping = True
                        label = self.session.rails.looping
                        self._say(ui.yellow(f"\n  stopped: {label} was called the same way "
                                            f"{guardrails.SAME_CALL_STOP} times in this turn")
                                  + ui.dim("\n  The results were delivered each time; the model "
                                           "kept asking. Try a more specific instruction, or "
                                           "`/model opus` for this one.\n"))
                        await client.interrupt()
            # Keys pressed during the turn were read here, not by the terminal.
            # Hand them to the next prompt so they are not silently eaten.
            self._typed_ahead = keys.typed_ahead.strip("\r\n")
            if keys.interrupted:
                self._say(ui.yellow("  stopped\n"))
                if self.control and self.control.active:
                    self.control.status("stopped from the terminal", level="warn")
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
        else:
            await self._context(client, brief=True)
        finally:
            if self.control and self.control.active:
                self.control.waiting = False
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            self._clear_activity()
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    def _print_recap(self) -> None:
        """Show the tail of a resumed conversation.

        `scivo -c` printed the same banner as a fresh session, so there was no
        way to tell whether it had picked up the conversation you meant.
        """
        if not self.session.resumed:
            return
        from . import sessions

        turns = sessions.recap(self.session.resumed)
        if not turns:
            print(ui.dim(f"  continuing {self.session.resumed[:8]} — no transcript to show\n"))
            return
        print(ui.dim(f"  continuing {self.session.resumed[:8]}, from where it left off:"))
        for role, text in turns:
            who = "you" if role == "user" else "scivo"
            body = " ".join(text.split())
            if len(body) > 240:
                body = body[:240] + " …"
            print(ui.dim(f"    {who:>5}  ") + ui.dim(body))
        print()

    async def run(self) -> None:
        briefing = self.session.briefing
        label = self.session.model
        if self.session.provider.is_local:
            label += f" @ {self.session.provider.name}"
        print(ui.banner(briefing.project_name, briefing.project_id,
                        self.session.plan.summary(), label))
        for line in ui.attention(briefing):
            print(line)
        if self.session.local_tools:
            print(ui.dim(f"  local kit: {self.session.local_tools} scivo tools"
                         + ("" if self.session.plan.profile != "full"
                            else " — `--profile video | deck | analysis` loads that domain too")))
        if self.session.endpoint is not None and self.session.endpoint.note:
            print(ui.dim(f"  · {self.session.endpoint.note}"))
        print()
        self._print_recap()

        self.client = ClaudeSDKClient(options=self.session.options)
        await self.client.connect()
        try:
            if True:
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
                        if self.control and self.control.active and via != "web":
                            self.control.user(line, via)
                        await self._permissions(self.client, command, argument.strip())
                        continue
                    if command == "/context":
                        if self.control and self.control.active and via != "web":
                            self.control.user(line, via)
                        await self._context(self.client, brief=False)
                        continue
                    if command == "/model":
                        if self.control and self.control.active and via != "web":
                            self.control.user(line, via)
                        await self._model(argument.strip())
                        continue
                    if self.control and self.control.active and via != "web":
                        self.control.user(line, via)
                    try:
                        if self._run_local(line):
                            continue
                    except EOFError:
                        break

                    await self._turn(self.client, line)
        finally:
            if self.control and self.control.active:
                await self.control.stop()
            await self.client.disconnect()
            if self.session.endpoint is not None:
                self.session.endpoint.close()
        print(ui.dim(f"session total ${self.cost:.4f}"))
        if self.session_id:
            print(ui.dim(f"resume: scivo resume {self.session_id[:8]}   (or scivo -c)"))
