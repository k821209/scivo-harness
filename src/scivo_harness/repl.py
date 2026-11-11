"""The interactive loop."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    CLIConnectionError,
    ConversationResetMessage,
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    TaskNotificationMessage,
    TaskUpdatedMessage,
    UserMessage,
)

from . import guardrails, ui
from .prompt import HANDOFF_ASK
from .pump import MessagePump, PumpClosed, StreamEnded
from .control import Control, merge_web_messages
from .preflight import to_markdown
from .failures import _why_it_stopped, explain
from .interrupts import KeyWatcher
from . import attachments as _attachments
from .prompt_line import Line
from .permissions import is_outward
from .session import Session, scivo_tool_label
from .toolsets import PREFIX

# Defined in session.py, beside DEFAULT_MODEL which derives from it. Re-exported
# here because this is where `/model` reads it.
from .session import CLAUDE_CATALOG, CLAUDE_MODELS  # noqa: F401
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
    "/doctor": "what pays for this session, and what could stop it",
    "/session": "this session's id, to resume it later",
    "/model": "switch model — on its own it lists every choice, numbered",
    "/handoff": "write the session handoff to Scivo now (automatic before a compaction)",
    "/notify": "what a finished background task does: off | ask | always",
    "/permissions": "show or change: default · acceptEdits · auto · plan · always <tool> · always -<tool>",
    "/dangerously-skip-permissions": "stop asking for anything (`off` to ask again); guardrails still apply",
    "/tools": "which scivo tools this profile loaded",
    "/profile": "narrow or widen the scivo tools this session carries — on its own it lists them, numbered",
    "/context": "how much of the context window is in use, and what fills it",
    "/paste": "attach the image on the clipboard to your next message",
    "/img": "attach an image file to your next message: /img <path>  (a bare image path does too)",
    "/compact": "summarise the conversation so far and carry on with a shorter one",
    "/clear": "start a fresh conversation — the way out when the old one no longer fits",
    "/update": "leave, run `scivo update` (`/update video` includes vh), and come back into this session with the page on",
    "/scivo-control": "drive this session from the scivo web page (`off` to stop, `take` to move the page here from another session)",
    "/help": "this list",
}

ANSI = re.compile(r"\x1b\[[0-9;]*m")

NOTIFY_MODES = ("off", "ask", "always")

# Task ids this project's sessions watched start, so a RESTARTED session can
# still tell its own background work from somebody else's. Kept beside the
# other per-project state (.scivo/control.json, .scivo/settings.json) and
# pruned as tasks reach a terminal state.
TASKS_FILE = Path(".scivo") / "tasks.json"
TASKS_KEPT = 200
# A task's output file is written by the CLI at
#   /tmp/claude-<uid>/<cwd-slug>/<session-id>/tasks/<task-id>.output
# so the PATH names the session that owns it — evidence from where the file
# actually landed, which is stronger than the `session_id` field on the notice
# (that one once carried our id for a task that existed nowhere on this
# machine). Matched as a uuid-shaped segment so a layout change degrades to
# "cannot tell" rather than to a wrong answer.
SESSION_IN_PATH = re.compile(
    r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")
# Where those per-session folders live. A name, so a test can point it
# somewhere else instead of depending on the host's own /tmp.
TASK_TMP = Path("/tmp")

# Tools that create a run row worth following (bare names; the scivo prefix is
# already stripped by `scivo_tool_label`).
RUN_CREATING_TOOLS = frozenset({"submit_remote_job", "launch_local_job",
                                "record_analysis_run"})

# Tools that edit one place in a manuscript. The dashboard draws a marker on
# it while the session works there — the one thing another editor had that
# ours did not: you could see where the agent was (user, 2026-11-09). Named
# rather than pattern-matched: `add_figure` also takes a slug and edits
# nothing a reader is looking at, and a marker on the wrong paragraph is worse
# than none.
EDITING_TOOLS = frozenset({"update_section", "replace_in_section", "add_section",
                           "resolve_paper_comment", "update_review"})
# Long enough to cost nothing next to an 11-minute job, short enough that the
# answer arrives while the person is still thinking about it.
RUN_POLL_SECONDS = 30.0
# Consecutive refusals before the watch gives up. An MCP without `run_notices`
# refuses every time and should stop being asked; a dropped pipe should not
# cost the notification for a job that is still running.
RUN_POLL_GIVE_UP = 3

# Typed at the offer, these mean "yes, pick it up". "확인" is in the list
# because that is the word the user was already typing by hand to make the
# model notice a finished job (2026-11-03).
NOTIFY_YES = frozenset({"y", "ye", "yes", "ok", "okay", "go", "sure",
                        "ㅇ", "ㅇㅇ", "ㄱ", "ㄱㄱ", "네", "예", "응", "확인"})


def _wake_summary(notes: list[str], *, name: int = 2, clip: int = 90) -> str:
    """What finished, short enough to be a prompt and a card title.

    Two separate things were sized wrong by the same oversight. The automatic
    turn's text was bounded after five notifications made a 700-character
    "line" that crashed the dispatch loop — and the OFFER was left joining
    every note in full, so a session with 22 finished tasks put thousands of
    characters into the question on the control page and the card could not be
    approved at all (user, 2026-11-03, in another project). The offer is the
    one a person has to read and click, so it is the shorter of the two.

    One renderer for both: the turn and the question describing the same
    notifications differently is the next confusion.
    """
    if not notes:
        return "a background task finished"
    if len(notes) == 1:
        return notes[0][:clip * 2]
    shown = "; ".join(note[:clip] for note in notes[:name])
    rest = len(notes) - name
    return (f"{len(notes)} background tasks finished: {shown}"
            + (f" (+{rest} more above)" if rest > 0 else ""))


def _net_record(*args, **kwargs) -> None:
    """The shared outbound-call log — see netlog.py."""
    from .netlog import record

    record(*args, **kwargs)


def _notify_mode_from_env() -> str:
    """`SCIVO_NOTIFY=off|ask|always`, else ask. An unreadable value is ask,
    not a crash: this decides a convenience, not a permission."""
    value = os.environ.get("SCIVO_NOTIFY", "").strip().lower()
    return value if value in NOTIFY_MODES else "ask"

# How much of one resumed message is shown. Lines, not characters: the point of
# the recap is the shape, and a character budget spends it all on the first
# paragraph of a long reply.
RECAP_LINES = 14
RECAP_INDENT = " " * len("scivo> ")


def recap_block(role: str, text: str, limit: int = RECAP_LINES) -> list[str]:
    """One message of a resumed conversation, as the live session shows it."""
    lines = text.strip().splitlines() or [""]
    shown, dropped = lines[:limit], len(lines) - min(len(lines), limit)
    if role == "user":
        # At the prompt it was typed at, continuation lines under it, so a
        # resumed exchange still reads as an exchange.
        block = [ui.cyan("scivo> ") + shown[0]]
        block += [RECAP_INDENT + line for line in shown[1:]]
    else:
        block = list(shown)
    if dropped:
        block.append(ui.dim(f"  … {dropped} more line{'' if dropped == 1 else 's'}"))
    return block

MODE_TAGS = {"default": "", "acceptEdits": "edits", "auto": "auto", "plan": "plan",
             "bypassPermissions": "skip", "dontAsk": "dontAsk"}
MODE_ALIASES = {"default": "default", "ask": "default", "acceptedits": "acceptEdits",
                "edits": "acceptEdits", "auto": "auto", "plan": "plan",
                "bypass": "bypassPermissions", "bypasspermissions": "bypassPermissions",
                "skip": "bypassPermissions"}


def _billed_to_api(provider) -> bool:
    """True when this session's requests hit an API tab, not a subscription.

    A claude.ai plan is billed by the plan, not the request; showing the CLI's
    per-turn dollar figure to that user reads as a bill they will not receive.
    Only three shapes are billed per request:
      · a gateway provider that carries a real token (auth_token_env);
      · the built-in `anthropic` provider with ANTHROPIC_API_KEY /
        ANTHROPIC_AUTH_TOKEN in the environment;
    Everything else — a local llama-server, a subscription-signed CLI — is not.
    """
    import os
    # A provider with a real token (auth_token_env) is billed by that gateway.
    # `auth_token = "unused"` on a local server does not count.
    if provider.auth_token_env:
        return True
    if not provider.has_endpoint:
        # The built-in `anthropic` provider: billed only if the environment
        # names an API key.
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if os.environ.get(var):
                return True
    return False


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



def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def cache_note(message: Any) -> str:
    """The turn's prompt-cache figures — "cache 24.5k read · 1.2k new" — or ""
    when the endpoint reports none. The guide is ~25k tokens of the prefix;
    a "read" that size on every turn after the first is the cache working,
    and a "new" that size on a turn that changed nothing is the prefix being
    invalidated (a profile switch, an MCP update). Per-model figures win
    when the CLI sends them; the flat usage dict is the fallback."""
    read = new = 0
    per_model = getattr(message, "model_usage", None) or {}
    if per_model:
        for usage in per_model.values():
            read += int(usage.get("cacheReadInputTokens") or 0)
            new += int(usage.get("cacheCreationInputTokens") or 0)
    else:
        usage = getattr(message, "usage", None) or {}
        read = int(usage.get("cache_read_input_tokens") or 0)
        new = int(usage.get("cache_creation_input_tokens") or 0)
    if not read and not new:
        return ""
    return f"cache {_k(read)} read · {_k(new)} new"

class Repl:
    def __init__(self, session: Session, auto_control: bool = False) -> None:
        self.attachments: list = []   # images queued for the next message (/paste, /img, web)
        # Set by /update: the CLI then runs the update and re-executes scivo
        # into this same session, with the control page on if it was on.
        self.relaunch: dict | None = None
        self.auto_control = auto_control
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
        # What a finished background task does while the prompt sits idle.
        # `ask` is the default on purpose: a session that spends a turn — and
        # takes tool actions — while nobody is watching should be a thing you
        # chose, not a thing you inherited. The Event coalesces either way:
        # five notifications in a row are one offer, not five.
        self.notify_mode = _notify_mode_from_env()
        self._wake = asyncio.Event()
        self._wake_notes: list[str] = []
        # Runs this session launched, watched until they close. Started only
        # when a run-creating tool is actually called, and stopped the moment
        # nothing of ours is open: the cheapest poll is the one not made.
        # Task ids this process watched start. The DECISION is made by
        # `_task_ownership`, which reads this and the record on disk — this set
        # alone is emptied by a restart, so after `--resume` a session's OWN
        # long task reported as another session's when it finished, which is
        # the one case where the label is actively misleading: the work IS this
        # project's and the user can see it is (user, 2026-11-06). Nothing
        # preloads the file into here, deliberately — a second copy of the
        # answer is a second thing to keep true, and the one reader that
        # matters already consults both.
        self._own_tasks: set[str] = set()
        self._runs_task: asyncio.Task | None = None
        self._runs_since: str | None = None
        # Where this session filed its runs, so the watch reads those and
        # not the whole project — see _note_run_home.
        self._run_homes: set[tuple[str, str]] = set()
        self._run_homes_complete = True
        self._runs_scoped = True
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
                if time.monotonic() - self._last_output < 2.0 or self._stopping or ui.prompt_open:
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
                if label in RUN_CREATING_TOOLS:
                    self._note_run_home(block.input or {})
                    self._watch_runs()
                if label in EDITING_TOOLS and self.control:
                    args = block.input or {}
                    self.control.focus(
                        slug=str(args.get("slug") or args.get("paper") or ""),
                        # `key` on a section tool, `section` on a comment one.
                        section=str(args.get("key") or args.get("section") or ""),
                        block_id=str(args.get("block_id") or "") or None,
                        tool=label)
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

    async def _start_pump(self) -> None:
        self.pump = MessagePump(self.client.receive_messages(), self._on_idle_message)
        await self.pump.start()

    async def _stop_pump(self) -> None:
        pump = getattr(self, "pump", None)
        if pump is not None:
            await pump.stop()

    def _watch_runs(self) -> None:
        """Start following this session's runs, if we are not already.

        A remote job finishes, the row is closed, the dashboard knows — and the
        session that launched it was never told, so an agent blocked on
        `wait_for_run` for seven minutes and then reported a finished job as
        still running (feedback ecba6396d9b4). The MCP stamps each run with the
        client that recorded it and `run_notices` answers what closed; this is
        the loop that asks, between turns, without holding the session.
        """
        if self._runs_task is not None and not self._runs_task.done():
            return
        self._runs_task = asyncio.create_task(self._follow_runs(), name="scivo-run-watch")

    def _note_run_home(self, arguments: dict) -> None:
        """Remember WHERE a run was filed, from the call that filed it.

        `run_notices` finds this session's runs by reading every run of every
        analysis in the project unless it is told where they are: 800
        documents a call in one real project, every thirty seconds for as
        long as one run stayed open. The session made the call; it knows.
        A call that does not say (no slug, no analysis) switches the scope
        off for good — a watcher that looked in the wrong place would report
        a job still running after it had finished, which is the failure the
        watcher exists to end.
        """
        slug, name = arguments.get("slug"), arguments.get("analysis")
        if isinstance(slug, str) and slug and isinstance(name, str) and name:
            self._run_homes.add((slug, name))
        else:
            self._run_homes_complete = False

    async def _follow_runs(self) -> None:
        """Ask `run_notices` until nothing of ours is open, then stop.

        Errors are swallowed and end the loop rather than the session: this is
        a convenience, and an MCP that predates `run_notices` must not take the
        REPL down with it. The next submitted run starts a fresh loop.

        Asked over the control page's own MCP connection when that is up —
        this used to start a server process of its own every time, a third
        one per session at ~120 MB — and over one of its own only when there
        is no page, or the page's connection stops answering.
        """
        from .scivo_mcp import connect

        try:
            shared = self.control._client if (self.control and self.control.active) else None
            if shared is not None and await self._poll_runs(shared) == "done":
                return
            async with connect(self.session.config) as client:
                await self._poll_runs(client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a convenience must not end a session
            return

    async def _poll_runs(self, client) -> str:
        """One watch on one connection. "done": nothing of ours is open, or
        the server has no such tool. "lost": this connection stopped
        answering, so the caller may try another."""
        refused = 0
        while True:
            await asyncio.sleep(RUN_POLL_SECONDS)
            arguments: dict = {"since": self._runs_since} if self._runs_since else {}
            scoped = bool(self._runs_scoped and self._run_homes_complete and self._run_homes)
            if scoped:
                arguments["analyses"] = [list(pair) for pair in sorted(self._run_homes)]
            asked = time.monotonic()
            outcome = await client.call("run_notices", **arguments)
            if scoped and not outcome.ok and "analyses" in str(outcome.error):
                # An MCP from before 2026-11-11 refuses the argument. Read
                # the whole project, as it always did, for the rest of this
                # session — slower and billed, and still correct.
                self._runs_scoped = False
                arguments.pop("analyses", None)
                outcome = await client.call("run_notices", **arguments)
            # Same log the MCP writes its SSH calls to, so a dropped link can
            # be lined up against everything we were sending (todo
            # d27e0606a7bb).
            _net_record("mcp", host="firestore", kind="run_notices",
                        ms=int((time.monotonic() - asked) * 1000),
                        rc=0 if outcome.ok else 1)
            if not outcome.ok:
                # A blip must not end the watch — an eleven-minute job would
                # then finish unannounced because one poll lost the pipe. A
                # tool that is simply not there refuses every time, so a run
                # of them is the real signal.
                refused += 1
                if refused >= RUN_POLL_GIVE_UP:
                    return "done" if "unknown tool" in str(outcome.error).lower() else "lost"
                continue
            refused = 0
            # A scalar tool comes back as ONE content block: `items` is always
            # a list (one block per record), so the dict is `first`. Reading
            # `items` here looked fine and silently never found a notice.
            data = outcome.first if isinstance(outcome.first, dict) else {}
            self._runs_since = data.get("now") or self._runs_since
            for notice in (data.get("closed") or []):
                self._on_run_closed(notice)
            if not data.get("still_open"):
                return "done"

    def _on_run_closed(self, notice: dict) -> None:
        """One finished run: printed now, and offered like a finished task."""
        code = notice.get("exit_code")
        run_key = str(notice.get("run_key") or "")
        where = f"{notice.get('slug')}/{notice.get('analysis')}"
        verdict = "exit 0" if code == 0 else f"exit {code}"
        paint = ui.yellow if code == 0 else ui.red
        stamp = time.strftime("%H:%M:%S")
        print(paint(f"\n[{stamp}]") + paint(f"  · run {run_key} {verdict}  {where}"),
              flush=True)
        if notice.get("caution"):
            print(ui.dim(f"    {notice['caution']}"), flush=True)
        if self.control and self.control.active:
            self.control.status(f"run {run_key} {verdict} ({where})",
                                level="info" if code == 0 else "warn")
        if self.notify_mode == "off":
            return
        note = f"run {run_key} in {where} finished, {verdict}"
        if notice.get("caution"):
            note += " — nothing recorded whether it worked, so read the log"
        if notice.get("log_path"):
            note += f" ({notice['log_path']})"
        self._wake_notes.append(note)
        self._wake.set()

    def _on_idle_message(self, message) -> None:
        """A message that arrived between turns — printed now, above the
        prompt (prompt_toolkit's patch_stdout keeps the input line intact).
        Task notifications are the point: a Monitor's event or a finished
        background job used to wait for the next user message."""
        if isinstance(message, TaskNotificationMessage):
            self._on_task_note(message)
        elif isinstance(message, TaskUpdatedMessage):
            self._on_task_update(message)
        elif isinstance(message, SystemMessage):
            self._on_system(message)
        # Anything else between turns has no turn to belong to; nothing to show.

    def _tasks_file(self) -> Path:
        return self.session.config.root / TASKS_FILE

    def _read_task_store(self) -> dict:
        path = self._tasks_file()
        if not path.is_file():
            return {}
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return stored if isinstance(stored, dict) else {}

    def _stored_task_ids(self) -> set[str]:
        """Task ids a session with THIS id watched start, from an earlier process.

        Keyed by session id, so a resume — which keeps the id — gets its own
        tasks back, and a different session in the same project does not
        inherit them.
        """
        if not self.session_id:
            return set()
        return {task for task, row in self._read_task_store().items()
                if isinstance(row, dict) and row.get("session") == self.session_id}

    def _write_task_store(self, store: dict) -> None:
        # Bounded: terminal tasks are dropped as they finish, and the file is
        # capped so a long-lived project cannot grow one without end.
        if len(store) > TASKS_KEPT:
            ordered = sorted(store.items(), key=lambda kv: str(kv[1].get("at") or ""))
            store = dict(ordered[-TASKS_KEPT:])
        path = self._tasks_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(store, indent=1), encoding="utf-8")
        except OSError:
            pass          # never let bookkeeping break a session

    def _remember_task(self, task_id: str) -> None:
        self._own_tasks.add(task_id)
        if not self.session_id:
            return
        store = self._read_task_store()
        store[task_id] = {"session": self.session_id,
                          "at": datetime.now(timezone.utc).isoformat()}
        self._write_task_store(store)

    def _forget_task(self, task_id: str) -> None:
        """A task that reached a terminal state needs no record."""
        store = self._read_task_store()
        if store.pop(task_id, None) is not None:
            self._write_task_store(store)

    def _session_in_output(self, output: str) -> str:
        """The session id the CLI's own output path puts this task under, if any."""
        found = SESSION_IN_PATH.search(output or "")
        return found.group(1) if found else ""

    def _task_on_disk(self, task_id: str) -> bool:
        """Is there a file for this task under THIS session's own task folder?

        Asked of the filesystem rather than of the notice, because a stopped
        background command's notification carries no `output_file` at all —
        which is how a task of this session's own came back "could not be
        determined" a second time (user, 2026-11-08: a `Background command …
        was stopped after reaching its` time limit).

        Globbed on the session id, not built from the cwd: the slug's spelling
        is the CLI's business, and a layout we cannot match returns False,
        which costs an "unknown" rather than buying a wrong answer.

        ONE-WAY evidence. Present proves the task is ours; ABSENT proves
        nothing — the session may be on another machine, and these files are
        temporary. So the caller may only read True.
        """
        if not task_id or not self.session_id:
            return False
        try:
            pattern = f"claude-*/*/{self.session_id}/tasks/{task_id}.*"
            return any(TASK_TMP.glob(pattern))
        except OSError:
            return False

    def _task_ownership(self, task_id: str, claimed: str,
                        output: str = "") -> str:
        """"own" | "foreign" | "unknown" — three states, because there are three.

        This was a bool, and the two readings it could not tell apart were
        "someone else's" and "mine, from before this process started". The
        second one printed "this session never saw it start", which is true and
        reads as "not yours", and the user could see the work was in fact
        theirs. `session_id` on the notice cannot settle it either: a task that
        exists nowhere on this machine once arrived carrying OUR id. So when
        the two disagree, say so rather than pick.
        """
        if task_id and task_id in self._own_tasks:
            return "own"
        row = self._read_task_store().get(task_id) if task_id else None
        if isinstance(row, dict) and row.get("session") == self.session_id:
            return "own"
        if isinstance(row, dict) and row.get("session"):
            return "foreign"            # another session here watched it start
        # Where the CLI actually put the output. This is what settles a task
        # that was already running when the record did not exist yet — an
        # upgrade, or any restart before `.scivo/tasks.json` was written
        # (user, 2026-11-06: an in-flight pbsim task came back "could not be
        # determined", and its output file was under the session's own folder
        # the whole time).
        wrote = self._session_in_output(output)
        if wrote and self.session_id:
            return "own" if wrote == self.session_id else "foreign"
        # Nothing on the notice to go on — look for the file ourselves. Only a
        # hit counts; see _task_on_disk on why a miss is not foreignness.
        if self._task_on_disk(task_id):
            return "own"
        if claimed and self.session_id and claimed != self.session_id:
            return "foreign"
        return "unknown"

    def _on_task_note(self, message) -> None:
        """A background task finished, failed, or was stopped — show it live.

        Without this, notifications sat unnoticed in the trace until the
        user typed something and the model then reported on them in a batch.
        Now the terminal shows them the moment the CLI delivers them.
        """
        stamp = time.strftime("%H:%M:%S")
        status = str(getattr(message, "status", ""))
        task = str(getattr(message, "task_id", ""))[:12]
        summary = str(getattr(message, "summary", "")).strip().replace("\n", " ")[:120]
        # Whose task this was. The CLI has always said — `session_id` is a field
        # on the message — and this read `task_id`/`status`/`summary` and threw
        # the rest away. Four notifications for tasks this session never
        # started arrived with "pick the work back up" attached, and the only
        # thing that stopped a plausible invented report was the agent
        # distrusting its own instructions (feedback e6c90056c2f8).
        where = str(getattr(message, "session_id", "") or "")
        output = str(getattr(message, "output_file", "") or "")
        # Not `session_id`: a notice naming a task that exists nowhere on this
        # machine arrived with OUR session id on it, so that field does not
        # answer "whose task is this". The answer is whether we watched it
        # start.
        full = str(getattr(message, "task_id", "") or "")
        owned = self._task_ownership(full, where, output)
        mine = owned == "own"
        paint = ui.red if status in ("failed", "errored") else ui.yellow
        line = f"  · task {task} {status}" + (f" — {summary}" if summary else "")
        print(paint(f"\n[{stamp}]") + " " + paint(line), flush=True)
        if owned == "foreign":
            # Printed, because it is true and may matter; never acted on, and
            # it says WHY — a feature that goes quiet for a reason nobody can
            # see is worse than one that is wrong out loud.
            print(ui.dim(f"    another session started task {full[:12] or '?'}"
                         f" (it says session {where[:8] or 'unknown'})"
                         f" — not picked up here"), flush=True)
        elif owned == "unknown":
            # NOT "another session's". Most often this session's own task from
            # before a restart, with the record lost; stated as the open
            # question it is, with what to do about it.
            # Only real things are named here: the notice's own session field,
            # and `/session` for this one's id. An earlier draft pointed at a
            # `/tasks` command that does not exist.
            print(ui.dim(f"    cannot tell whose task {full[:12] or '?'} is — this "
                         f"process never saw it start, and no record here "
                         f"claims it"
                         + (f"; the notice says session {where[:8]}" if where
                            else "; the notice names no session")
                         + ". If this session was resumed, it is most likely "
                         "its own (/session shows this session's id). "
                         "Not picked up until you say so."), flush=True)
        if self.control and self.control.active:
            label = {"own": "", "foreign": " — another session's, not handled here",
                     "unknown": " — whose task this is could not be determined"}[owned]
            self.control.status(f"task {task} {status}: {summary}{label}", level="warn")
        if full and status not in ("running", "queued", "starting", "resumed"):
            self._forget_task(full)
        # A FOREGROUND call's completion is not work to pick up: its result was
        # already handed back inside the turn, so an offer to "carry on with
        # it" is a loop. Five such notices arrived naming this session's own
        # Bash descriptions verbatim — "Make the fixtures watch tasks start" —
        # for calls whose output this conversation had already used
        # (2026-11-05). A real background task writes an output file and these
        # do not, which is the cheapest line between the two.
        backgrounded = bool(output)
        if mine and not backgrounded:
            print(ui.dim(f"    task {full[:12]} ran in the foreground — its result "
                         f"is already in this turn, nothing to pick up"), flush=True)
        if self.notify_mode != "off" and mine and backgrounded:
            # Printing it was only half the job: the model is not in a turn, so
            # nothing acts on it until the user says something. `/notify off`
            # goes back to printing only.
            note = f"task {task} {status}" if task else str(status)
            if summary:
                note += f" ({summary})"
            if output:
                note += f", output in {output}"
            self._wake_notes.append(note)
            self._wake.set()

    def _consume_wake(self) -> list[str]:
        """The notifications waiting for a turn, taken and cleared.

        Cleared on EVERY turn, not only an automatic one: a notification that
        arrives while the user is typing is already in the conversation by the
        time their line is sent, so the model sees it in that turn and must not
        be woken again for it afterwards.
        """
        notes, self._wake_notes = self._wake_notes, []
        self._wake.clear()
        return notes

    def _wake_line(self, notes: list[str]) -> str:
        """What the automatic turn says. Prefixed, because it was not typed.

        Deliberately thin: the notification itself is already in the
        conversation (that is why typing anything at all used to produce a
        report), so this only has to say that nobody is waiting to be asked.
        """
        what = _wake_summary(notes)
        # "this session's own" is a checked claim, not a reassurance: a
        # notification whose session_id is not ours never reaches here.
        return (f"[scivo] {what} — this session's own task, and the notification "
                f"is above in this conversation. Nobody typed this: pick the work "
                f"back up, or say what you found and stop.")

    async def _offer_wake(self, prompt: str, plain: str, typed: str) -> tuple[str, str]:
        """Ask before spending a turn on it — the default.

        One prompt does both jobs, so there is no second input channel to get
        wrong: a bare yes picks the work up, any other words become the turn
        (which carries the notification just the same, since it is already in
        the conversation), and an empty line declines. Whatever was half-typed
        arrives as the default, so the offer cannot eat it either.
        """
        notes = self._consume_wake()
        what = _wake_summary(notes)
        if self.control is not None and self.control.active:
            # The page renders the question card and collects the answer. Asking
            # in the terminal while someone is on the page is the mistake
            # `permissions._questions` already records: the page existed and was
            # never called, so the person at the page had to walk to the
            # terminal. No terminal prompt is drawn here, so anything typed
            # before goes back to pending.
            self._typed_ahead = typed
            reply = await self._offer_on_page(what)
        else:
            print(ui.yellow(f"\n  {what}"), flush=True)
            reply = (await self.line.ask(
                ui.yellow("  pick it up? ") + ui.dim("[y/N, or say what you want instead] "),
                "  pick it up? [y/N] ", default=typed)).strip()
        if reply.lower() in NOTIFY_YES:
            line = self._wake_line(notes)
            print(f"{prompt}{line}  {ui.dim('[task]')}", flush=True)
            return line, "task"
        return reply, "terminal"

    # The card's own wording. `_PICK_UP` doubles as the yes: whatever comes
    # back is compared against it, so the label and the test cannot drift.
    _PICK_UP = "Pick it up"
    _NOT_NOW = "Not now"

    async def _offer_on_page(self, what: str) -> str:
        """The same offer, as a question card on the scivo-control page.

        Returns a yes word, free text the owner typed instead, or "" when the
        card was skipped — the shapes `_offer_wake` already handles.
        """
        question = f"{what} — pick it up?"
        answers = await self.control.ask_question({"questions": [{
            "question": question,
            "header": "background task",
            "options": [
                {"label": self._PICK_UP,
                 "description": "start a turn on it now"},
                {"label": self._NOT_NOW,
                 "description": "leave it — say something else when you want to"},
            ],
        }]})
        picked = (answers or {}).get(question)
        if isinstance(picked, list):
            picked = picked[0] if picked else ""
        picked = str(picked or "").strip()
        if picked == self._PICK_UP:
            return "y"
        if picked == self._NOT_NOW:
            return ""
        # Their own words, typed into the card instead of choosing: that
        # becomes the turn, exactly as it does in the terminal.
        return picked

    def _announce_wake(self, prompt: str) -> str:
        """Show the automatic turn where a typed line would have been.

        A turn that appears with no visible prompt line reads as the model
        talking to itself.
        """
        line = self._wake_line(self._consume_wake())
        print(f"{prompt}{line}  {ui.dim('[task]')}", flush=True)
        return line

    def _on_task_update(self, message) -> None:
        patch = getattr(message, "patch", {}) or {}
        status = str(patch.get("status") or getattr(message, "status", "") or "")
        # A task this session watched START is a task this session owns. This
        # is the ownership test now: `session_id` on a completion notice
        # claimed one that exists nowhere on this machine as "this session's
        # own", so filtering on it is not a filter (2026-11-04). What we saw
        # with our own eyes, we keep.
        started = str(getattr(message, "task_id", "") or "")
        if started and status in ("running", "queued", "starting", "resumed"):
            # Written down, not just remembered: see _task_ownership.
            self._remember_task(started)
        # Terminal state updates only; running/queued spam the log.
        if status in ("running", "queued", "starting", "resumed", ""):
            return
        stamp = time.strftime("%H:%M:%S")
        task = str(getattr(message, "task_id", ""))[:12]
        where = str(getattr(message, "session_id", "") or "")
        whose = "" if where == self.session_id else f"  (session {where[:8] or 'unknown'})"
        print(ui.dim(f"\n[{stamp}]  · task {task} {status}{whose}"), flush=True)

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
        cache = cache_note(message)
        self.turns += message.num_turns
        # Claude Code prices every turn from its own table, including models it
        # does not know: a Qwen turn on a local server came back as $0.334.
        # A claude.ai subscription is billed by the plan, not the request, so
        # the dollar figure the CLI reports is what an equivalent API call
        # would cost — the user is not paying it. Only show a price when the
        # session is actually API-billed. That table holds Anthropic's models
        # and nothing else, so no total is kept for any endpoint of our own,
        # paid or free — what a gateway charges is on the gateway's bill.
        endpoint = self.session.provider.has_endpoint
        billed = _billed_to_api(self.session.provider)
        turn_cost = (message.total_cost_usd or 0.0) if (billed and not endpoint) else 0.0
        self.cost += turn_cost
        if message.is_error and not self._stopping:
            # A turn stopped on purpose comes back as an error with no text.
            # Reporting "! error:" for a stop the person asked for reads as a
            # fault in the harness.
            print(ui.red(f"\n  ! {message.stop_reason or 'error'}: {message.result or ''}"))
            from .providers import CREDENTIAL_REFUSAL
            if CREDENTIAL_REFUSAL in (message.result or ""):
                # The one error whose cause is a policy rather than a bug. It
                # would otherwise read as "the harness broke" on the day
                # Anthropic starts enforcing its credential terms against the
                # Agent SDK — which they published on 2026-02-19 and began
                # enforcing at the API layer, for third-party tools, on
                # 2026-01-09.
                print(ui.yellow(
                    "    Your claude.ai subscription is no longer accepted through the "
                    "Agent SDK, which is how every scivo session runs.\n"
                    "    Nothing here is broken and nothing you change locally will fix "
                    "it — but SCIVO is not affected. The MCP and the\n"
                    "    skills run in Claude Code itself on the same plan: open `claude` "
                    "in this directory and carry on. The harness\n"
                    "    is one host of four (`python -m co_scientist_local hosts`).\n"
                    "    If you would rather keep the harness, `--api-billing` uses an "
                    "exported ANTHROPIC_API_KEY and is metered\n"
                    "    per token, or `/model <provider>` points at a local endpoint. "
                    "`/doctor` shows which you are on."))
        if endpoint:
            # The CLI's dollar figure prices this turn off Anthropic's table,
            # which this model is not on, so name the provider instead and say
            # where the real cost lands.
            price = self.session.provider.name
            total = "metered on that account" if billed else "no cost"
            # What a local turn costs is time, and nearly all of it is the
            # prefill; the shim (when one is in the path) timed each call.
            from . import shim as _shim
            env = getattr(getattr(self.session, "endpoint", None), "env", None) or {}
            note = _shim.prefill_note(_shim.take_stats(env.get("ANTHROPIC_BASE_URL", "")))
            if note:
                total = note
        elif billed:
            price, total = f"${turn_cost:.3f}", f"${self.cost:.3f} session"
        else:
            # claude.ai subscription: no per-turn dollars, keep the shape.
            price, total = "claude.ai", "on your plan"
        print(ui.dim(f"\n  ({message.num_turns} turns · {price} · {total}"
                     + (f" · {cache}" if cache and not endpoint else "") + ")\n"))
        if self.control:
            self.control.result(message.num_turns, turn_cost, self.cost,
                                error=(message.stop_reason or "error") if message.is_error else None)

    # -------------------------------------------------------- local commands

    def _attach(self, command: str, argument: str) -> None:
        """`/paste`, `/img <path>`, or a bare image path: queue an image for
        the next message. Nothing is sent yet — type the message next."""
        root = self.session.config.root
        if command == "/paste":
            got = _attachments.read_clipboard_image()
            if not got:
                self._say(ui.red(f"  no image on the clipboard ({_attachments.clipboard_tool_hint()})"))
                return
            path = _attachments.save_inbound(root, got[0], got[1], "clip")
        else:
            path = _attachments.image_path_in(argument)
            if path is None:
                self._say(ui.red(f"  not an image file: {argument or '(no path given)'}"))
                return
        self.attachments.append(path)
        self._say(ui.dim(f"  attached {path.name} ({len(self.attachments)} pending) — now type the message"))

    def _local(self, line: str) -> bool:
        """True if the line was handled here and must not reach the model."""
        command = line.split()[0]
        if command not in LOCAL_COMMANDS:
            return False
        if command in {"/exit", "/quit"}:
            raise EOFError
        if command == "/update":
            # Leaving, updating and coming back was four commands typed by
            # hand — /exit, scivo update, scivo -c, /scivo-control — and the
            # last one was forgotten as often as not (user, 2026-09-30).
            # The mode and model are carried too, because they are part of
            # "this session" as much as the page is: both can be switched
            # mid-session (`/dangerously-skip-permissions`, `/model`), and
            # neither lives in argv, so an update silently handed the session
            # back in whatever mode it was STARTED in (user, 2026-10-19). The
            # model only travels for Claude models — `/model <provider>` starts
            # a new conversation, so there is no session to resume into.
            self.relaunch = {"resume": self.session_id,
                             "control": bool(self.control and self.control.active),
                             "video": "video" in line.split()[1:],
                             "permission_mode": self.mode,
                             "model": (None if self.session.provider.has_endpoint
                                       else self.session.model)}
            coming_back = [
                "with the control page back on" if self.relaunch["control"] else "",
                f"in {self.mode}" if self.mode != "default" else "",
            ]
            said = " and ".join(part for part in coming_back if part)
            print(ui.dim("\n  leaving to update; this session resumes when it is done"
                         + (f" — {said}" if said else "") + "\n"))
            raise EOFError
        if command == "/help":
            print()
            for name, description in LOCAL_COMMANDS.items():
                print(f"  {ui.cyan(name):<22} {description}")
            print(ui.dim("  Esc                    stop the turn that is running"))
            print(ui.dim(f"\n  and {len(self.skills)} skills — press / to list them"
                         + ("" if self.line.rich else " (see `scivo status`)") + "\n"))
        elif command == "/doctor":
            # The same lines `scivo doctor` prints, from inside the session —
            # asked for because the answer can change under a running session
            # (Anthropic's credential terms), and leaving to check it means
            # losing the session (user, 2026-10-25).
            from .providers import billing_lines
            print()
            print(f"  model     {self.session.model}")
            print(f"  provider  {self.session.provider.name}")
            print(f"  mode      {self.mode}")
            for line in billing_lines(self.session.provider):
                print(ui.dim(f"  billing   {line}") if line.startswith(("Anthropic", "Working"))
                      else f"  billing   {line}")
            print()
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
            if self.session.provider.has_endpoint:
                # Nothing was accumulated (see _on_result), so there is no
                # figure to print — saying $0.0000 to someone whose gateway is
                # metering them would be worse than saying nothing.
                paid = (" — metered on that account" if _billed_to_api(self.session.provider)
                        else ", no cost")
                print(ui.dim(f"\n  {self.session.provider.name} endpoint{paid}"
                             f" — {self.turns} turns\n"))
            elif _billed_to_api(self.session.provider):
                print(ui.dim(f"\n  ${self.cost:.4f} over {self.turns} turns\n"))
            else:
                print(ui.dim(f"\n  claude.ai subscription — {self.turns} turns, on your plan\n"))
        elif command == "/tools":
            plan = self.session.plan
            print(f"\n  {plan.summary()}")
            if self.session.lean_tools:
                print(ui.dim(f"  lean kit: {self.session.lean_tools} of them are loaded — a small "
                             "model reads every schema on every request."))
                print(ui.dim("  `scivo --profile video` (or deck, analysis) loads that domain too."))
            if plan.dropped:
                print(ui.dim(f"  dropped: {', '.join(plan.dropped[:12])}"
                             + (" …" if len(plan.dropped) > 12 else "")))
            print()
        return True

    # ------------------------------------------------------- scivo-control

    def _print_control_health(self) -> None:
        """Whether the page is actually keeping up — the question that had no
        answer when the page froze and the terminal carried on (2026-11-08)."""
        if not (self.control and self.control.active):
            return
        state = self.control.health()
        behind, errors = state["pending_chunks"], state["errors"]
        ago = state["last_flush_ago"]
        # How the page's messages get here. Said every time, because the two
        # answers differ by the whole Firestore bill and by how long a message
        # waits — and nothing else on screen tells them apart.
        if state.get("inbox") == "listening":
            print(ui.dim("  inbox     listening — messages arrive as they are written"))
        elif state.get("inbox"):
            why = f" ({state['inbox_why']})" if state.get("inbox_why") else ""
            print(ui.yellow(f"  inbox     {state['inbox']}, not listening{why}"))
        if not behind and not errors:
            when = "never" if ago is None else f"{ago:.0f}s ago"
            print(ui.dim(f"  page up to date ({state['events']} events, last "
                         f"write {when})\n"))
            return
        if behind:
            print(ui.yellow(f"  the page is BEHIND: {behind} chunk(s) written "
                            f"here and not yet on the page"))
        if ago is None:
            print(ui.yellow("  nothing has ever reached the page"))
        elif ago > 60:
            print(ui.yellow(f"  last successful write {ago:.0f}s ago"))
        if errors:
            print(ui.red(f"  {errors} error(s) from the page's own machinery; "
                         f"most recent:"))
            print(ui.dim(f"    {state['last_error']}"))
        print()

    async def _control(self, argument: str) -> None:
        if argument == "off":
            if self.control and self.control.active:
                await self.control.stop()
                print(ui.dim("\n  scivo-control off — the page is unpublished until you turn it on again\n"))
            else:
                print(ui.dim("\n  scivo-control is not on\n"))
            return

        take = argument == "take"
        if self.control and self.control.active:
            self._print_link()
            self._print_control_health()
            return

        briefing = self.session.briefing
        self.control = Control(self.session.config, briefing.project_name,
                               briefing.project_id, self.session.model,
                               extra_roots=tuple(
                                   getattr(self.session.options, "add_dirs", None) or ()))
        print(ui.dim("\n  publishing the control page…"))
        from .control import PageInUse
        try:
            await self.control.start(take=take)
        except PageInUse as exc:
            # Not a failure: the page is doing its job for someone else, and
            # the way to change that is one word.
            print(ui.yellow(f"  {exc}\n"))
            self.control = None
            return
        except Exception as exc:  # noqa: BLE001
            print(ui.red(f"  scivo-control failed to start: {exc}\n"))
            self.control = None
            return
        if self.control._claim_note:
            print(ui.dim(f"  {self.control._claim_note}"))
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
        on_page = bool(self.control and self.control.active)
        # A notification that landed before we got here needs no race.
        if self.notify_mode != "off" and self._wake.is_set():
            if self.notify_mode == "ask":
                # The offer's prompt carries what was typed, so it is not
                # stashed as well — that would send it twice.
                return await self._offer_wake(prompt, plain, typed)
            # No prompt was drawn, so anything typed during the last turn is
            # still pending: hand it back rather than consume it here.
            self._typed_ahead = typed
            return self._announce_wake(prompt), "task"
        # A plain stdin read runs in a thread that cannot be withdrawn, so
        # neither a web message nor a wake-up can interrupt it; the next Enter
        # carries them (SCIVO_PROMPT_STYLE=simple).
        if not on_page and not (self.notify_mode != "off" and self.line.rich):
            line = await self.line.ask(prompt, plain, default=typed)
            self._consume_wake()
            return line, "terminal"

        web = asyncio.create_task(self.control.messages.get()) if on_page else None
        wake = asyncio.create_task(self._wake.wait()) if self.notify_mode != "off" else None
        waiting = {task for task in (web, wake) if task is not None}
        terminal = None
        # Only the prompt_toolkit prompt can be cancelled cleanly.
        if self.line.rich:
            terminal = asyncio.create_task(self.line.ask(prompt, plain, default=typed))
            waiting.add(terminal)
        # What is half-typed at the prompt, read BEFORE anything is cancelled:
        # cancelling `prompt_async` drops its buffer, and a wake-up that ate a
        # half-written line would be worse than waiting.
        done, pending = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        half_typed = "" if terminal in done else self.line.pending_text()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        if terminal is not None and terminal in done:
            if web is not None and web in done:  # both at once: keep it for next time
                await self.control.messages.put(web.result())
            self._consume_wake()
            return terminal.result(), "terminal"
        if wake is not None and wake in done and (web is None or web not in done):
            if self.notify_mode == "ask":
                return await self._offer_wake(prompt, plain, half_typed)
            self._typed_ahead = half_typed
            return self._announce_wake(prompt), "task"
        self._consume_wake()
        got = web.result()
        # Anything else already queued goes in with it: the page may have
        # taken several lines while the previous turn ran, and the later ones
        # are usually the correction to the first. Only what is waiting RIGHT
        # NOW — this never blocks for more.
        queued = [got]
        while True:
            try:
                queued.append(self.control.messages.get_nowait())
            except asyncio.QueueEmpty:
                break
        if len(queued) > 1:
            got = merge_web_messages(queued)
            print(ui.dim(f"  [{len(queued)} web messages sent while the last turn ran, "
                         "taken together]"))
        if isinstance(got, dict):          # a web message with images attached
            self.attachments.extend(got.get("images") or [])
            text = got.get("text") or ""
            print(f"{prompt}{text}  {ui.dim('[web · ' + str(len(got.get('images') or [])) + ' image(s)]')}")
            return text or "(see the attached image)", "web"
        text = got
        print(f"{prompt}{text}  {ui.dim('[web]')}")
        return text, "web"

    # ---------------------------------------------------------------- notify

    def _notify(self, argument: str) -> None:
        want = argument.strip().lower()
        want = "always" if want == "on" else want
        if want and want not in NOTIFY_MODES:
            self._say(ui.red(f"\n  /notify takes {' | '.join(NOTIFY_MODES)}, not {want!r}\n"))
            return
        if want:
            self.notify_mode = want
            if want == "off":
                self._consume_wake()
        says = {"off": "is printed, and waits for you to say something",
                "ask": "is printed, and asks whether to pick it up",
                "always": "starts a turn by itself, with nobody asked"}
        self._say(f"\n  notify   {self.notify_mode}"
                  + ui.dim(f"  — a finished background task {says[self.notify_mode]}"
                           f"\n  /notify {' | '.join(NOTIFY_MODES)}"
                           f"\n  SCIVO_NOTIFY=<mode> to start every session that way\n"))

    # ----------------------------------------------------------------- model

    def _model_rows(self, providers: dict) -> list[tuple[str, str, str]]:
        """Everything this session can switch to: what to type, what it is.

        One list builds the numbered menu AND resolves `/model 3`, so the
        number a person reads cannot drift from the number that is picked.
        A provider is listed under its own name rather than its model id,
        because that is what the switch below takes.
        """
        rows = [(alias, model, f"Claude · {era}")
                for alias, (model, era) in CLAUDE_CATALOG.items()]
        rows += [(name, candidate.model or "(the provider's default)",
                  f"{name} · new conversation")
                 for name, candidate in providers.items() if name != "anthropic"]
        return rows

    async def _model(self, argument: str) -> None:
        from .providers import load_all
        from .session import DEFAULT_EFFORT, DEFAULT_MODEL

        provider = self.session.provider
        providers = load_all()
        rows = self._model_rows(providers)
        if not argument:
            token_width = max(len(token) for token, _, _ in rows)
            model_width = max(len(model) for _, model, _ in rows)
            listing = []
            for index, (token, model, note) in enumerate(rows, start=1):
                here = model == self.session.model
                listing.append(
                    f"\n  {index:>2}. " + ui.bold(token.ljust(token_width)) + "  "
                    + (ui.green(model.ljust(model_width)) if here else model.ljust(model_width))
                    + ui.dim(f"  {note}")
                    + (ui.green("  ← this session") if here else ""))
            self._say(f"\n  model     {self.session.model}"
                      + f"\n  provider  {provider.name}" + (f" → {provider.base_url}" if provider.base_url else "")
                      + "\n" + "".join(listing)
                      + ui.dim("\n\n  /model <number>, <name>, or any claude-* id"
                               + ("" if len(providers) > 1 else
                                  "\n  no other endpoint configured — scivo providers --init")
                               + "\n  to make a provider stick for this project: scivo providers use <name>\n"))
            return

        wanted = argument.split()[0]
        if wanted.isdigit():
            # The menu's own numbering, read back from the list that printed it.
            choice = int(wanted)
            if not 1 <= choice <= len(rows):
                self._say(ui.red(f"\n  there is no choice {choice}")
                          + ui.dim(f" — /model lists {len(rows)}\n"))
                return
            wanted = rows[choice - 1][0]
        claude_model = CLAUDE_MODELS.get(wanted.lower()) or (wanted if wanted.startswith("claude-") else None)

        if claude_model and not provider.has_endpoint:
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

        if target.has_endpoint:
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
        try:
            await self._swap_client(options)
        except Exception as exc:  # noqa: BLE001
            endpoint.close()
            self._say(ui.red(f"  could not start on {target_name}: {exc}"))
            # Back where it was — and INTO the conversation it was having,
            # which is on disk and did not go anywhere.
            await self._swap_client(self._resumed(self.session.options))
            kept = ("the conversation continues" if self.session_id
                    else "in a new conversation — there was no session id to return to")
            self._say(ui.dim(f"  back on {provider.name} ({self.session.model}), {kept}\n"))
            return
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

    def _resumed(self, options):
        """`options`, pointed back at THIS conversation when there is one.

        Options are a snapshot from when they were built, and `resume` in
        them is whatever the session was started with — usually nothing. A
        fallback that reconnected with them as they stood came back into an
        EMPTY conversation, and `/profile` said only "still on full".
        """
        if not self.session_id:
            return options
        from dataclasses import replace

        return replace(options, resume=self.session_id, continue_conversation=False)

    async def _swap_client(self, options) -> None:
        """Replace the CLI process with one started from `options`.

        The one place this is done. A model switch, a profile switch and a
        reconnect each had their own copy, and the copies had drifted: one
        suppressed a failed disconnect and two did not, so a dead CLI could
        fail the very step meant to replace it. Raises what `connect` raises,
        and then there is NO live client — the caller decides what to fall
        back to.
        """
        await self._stop_pump()
        with contextlib.suppress(Exception):     # the old process may already be gone
            await self.client.disconnect()
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()
        await self._start_pump()
        if self.mode != (options.permission_mode or "default"):
            try:
                await self.client.set_permission_mode(self.mode)
            except Exception as exc:  # noqa: BLE001 — connected; the mode is worth a line, not the session
                self._say(ui.yellow(f"  could not restore permission mode {self.mode}: {exc}"))

    async def _reconnect(self, exc: Exception) -> bool:
        """Start a new CLI on the same conversation. False means give up.

        The transcript lives on disk, so a terminated process costs the
        process and not the session — provided there is a session id to resume
        from. Without one there is nothing to come back to, and saying so
        beats reconnecting into an empty conversation.
        """
        if isinstance(exc, StreamEnded):
            # The process may well be alive; what is gone is our reading of
            # it. Saying "the process stopped" would send someone to look for
            # a kill that did not happen.
            self._say("\n" + ui.red(f"  stopped hearing from Claude Code: {exc}"))
            self._say(ui.dim("  Its output stream ended on this side, so nothing "
                             "it says can be read. Starting a new one on the "
                             "same conversation."))
        else:
            self._say("\n" + ui.red(f"  the Claude Code process stopped: {exc}"))
            self._say(ui.dim("  " + _why_it_stopped(str(exc))))
        if not self.session_id:
            self._say(ui.dim("  this session had no id to resume — nothing to "
                             "reconnect to. Start again with `scivo`.\n"))
            return False
        self._say(ui.dim(f"  reconnecting to {self.session_id[:8]}…"))
        options = self._resumed(self.session.options)
        try:
            await self._swap_client(options)
        except Exception as again:  # noqa: BLE001
            self._say(ui.red(f"  could not reconnect: {again}"))
            self._say(ui.dim(f"  the conversation is kept — "
                             f"`scivo resume {self.session_id[:8]}`\n"))
            return False
        self.session.options = options
        self._say(ui.green("  reconnected") + ui.dim(" — the conversation continues. "
                                                     "The turn that was running did not finish; "
                                                     "say it again.\n"))
        return True

    # -------------------------------------------------------------- profile

    def _profile_rows(self) -> list[tuple[str, int, str]]:
        """Each profile, with how many of THIS server's tools it would keep."""
        from . import toolsets

        rows = []
        for name in toolsets.PROFILES:
            try:
                plan = toolsets.plan(list(self.session.scivo_tools), profile=name,
                                     read_only=self.session.plan.read_only_session)
            except ValueError:
                continue
            domains = ", ".join(toolsets.PROFILES[name])
            # An empty column reads as a rendering gap rather than as the
            # profile's whole point.
            rows.append((name, len(plan.kept),
                         domains or "nothing — the host's own tools only"))
        return rows

    async def _profile(self, argument: str) -> None:
        """Switch the tool surface without losing the conversation.

        The profile is `allowed_tools` / `disallowed_tools` on the options, and
        those are read when the CLI starts — so this rebuilds the client, the
        way a provider switch does. Unlike that one it RESUMES: same endpoint,
        same model, so there is nothing about the history the new process
        cannot take, and the point of switching mid-session is to keep going
        with a different kit (user, 2026-11-08).
        """
        from . import prompt as prompt_mod
        from . import toolsets

        rows = self._profile_rows()
        current = self.session.plan.profile
        if not argument:
            width = max(len(name) for name, _, _ in rows)
            listing = []
            for index, (name, kept, domains) in enumerate(rows, start=1):
                here = name == current
                listing.append(
                    f"\n  {index:>2}. " + ui.bold(name.ljust(width))
                    + (ui.green(f"  {kept:>3} tools") if here else f"  {kept:>3} tools")
                    + ui.dim(f"  {domains}")
                    + (ui.green("  ← this session") if here else ""))
            self._say(f"\n  {self.session.plan.summary()}"
                      + "".join(listing)
                      + ui.dim("\n\n  /profile <number or name>"
                               "\n  this scopes the scivo TOOLS; skills stay available in every"
                               " profile, and /tools lists what is loaded now\n"))
            return

        wanted = argument.split()[0].lower()
        if wanted.isdigit():
            choice = int(wanted)
            if not 1 <= choice <= len(rows):
                self._say(ui.red(f"\n  there is no choice {choice}")
                          + ui.dim(f" — /profile lists {len(rows)}\n"))
                return
            wanted = rows[choice - 1][0]
        if wanted not in toolsets.PROFILES:
            self._say(ui.red(f"\n  no profile called {wanted!r}")
                      + ui.dim(f" — {', '.join(toolsets.PROFILES)}\n"))
            return
        if wanted == current:
            self._say(ui.dim(f"\n  already on {wanted} — {self.session.plan.summary()}\n"))
            return

        plan = toolsets.plan(list(self.session.scivo_tools), profile=wanted,
                             read_only=self.session.plan.read_only_session)
        before_plan, before_options = self.session.plan, self.session.options
        # `options_for` consults `session.plan.profile` on the lean path, so the
        # new plan has to be in place before the options are built — and put
        # back if the new client will not start.
        self.session.plan = plan
        # The append names the profile and counts its tools, so it is rebuilt
        # too — left alone it would tell the model it is running the kit it had
        # a moment ago. The guide is handed over unconditionally and `build`
        # decides: it drops both guides when nothing is loaded, since each is
        # instructions for calling tools that are now absent. Deciding it here
        # as well was a second copy of the rule that no test could see.
        options = self.session.options_rekitted(
            plan,
            full_append=prompt_mod.build(self.session.briefing, plan,
                                         guide=self.session.guide),
            lean_append=prompt_mod.build(self.session.briefing, plan, guide=None),
            resume=self.session_id,
        )
        self._say(ui.dim(f"\n  switching to {wanted} — {plan.summary()}…"))
        try:
            await self._swap_client(options)
        except Exception as exc:  # noqa: BLE001
            self.session.plan = before_plan
            self._say(ui.red(f"  could not switch profile: {exc}"))
            # `before_options` as they stood would resume whatever the session
            # was STARTED with — usually nothing — so "still on full" used to
            # mean "on full, in an empty conversation", with no word of it.
            await self._swap_client(self._resumed(before_options))
            kept = ("the conversation continues" if self.session_id
                    else "in a NEW conversation — there was no session id to return to")
            self._say(ui.dim(f"  still on {before_plan.profile}; {kept}\n"))
            return
        self.session.options = options
        kept = ("the conversation continues" if self.session_id
                # Said plainly rather than discovered: without an id to resume,
                # the rebuilt process starts empty, and that is worth knowing
                # BEFORE typing the next message into it.
                else "a NEW conversation — this session had no id to resume")
        self._say(ui.green(f"  {plan.summary()}") + ui.dim(f" — {kept}\n"))

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
            kinds = sorted(getattr(approver, "shell_rules", None) or ())
            self._say("\n  mode    " + self.mode
                      + "\n  always  " + (", ".join(always) if always else "(none)")
                      + ("\n  shell   " + ", ".join(f"{kind} …" for kind in kinds) if kinds else "")
                      + ("\n  shell   read-only session — every command is asked about"
                         if getattr(approver, "read_only", False) else "")
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
                    if name in ("Bash", "Monitor"):
                        # The kinds remembered by answering `a` go with it:
                        # "asks again" has to be true of every command.
                        approver.shell_rules.clear()
                    self._say(ui.dim(f"  {label}: asks again"))
                elif name in ("Bash", "Monitor") and getattr(approver, "read_only", False):
                    self._say(ui.yellow(f"  {label} is asked about every time in a read-only "
                                        "session; restart without --read-only to change that."))
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

    async def _guarded_turn(self, line: str) -> bool:
        """One turn, and a new CLI if this one is gone. False: give up.

        Both ways a session loses its CLI end here. `CLIConnectionError` is
        the process having exited — writing to it raised out of run(), out of
        main, and printed a traceback ending in "Cannot write to terminated
        process", the least useful thing a person can be shown when the
        conversation is safe on disk and the remedy is one line (user,
        2026-11-09, exit 143 on spark-a404). `StreamEnded` is the quieter
        one: the process is still there and nothing it says reaches us.
        `/handoff` runs a turn too and used to do it outside this, so the one
        command meant for a session in trouble was the one that crashed.
        """
        try:
            await self._turn(self.client, line)
        except StreamEnded as exc:
            return await self._reconnect(exc)
        except CLIConnectionError as exc:
            return await self._reconnect(exc)
        return True

    async def _turn(self, client: ClaudeSDKClient, line: str) -> None:
        pump = getattr(self, "pump", None)
        if pump is not None and pump.ended:
            # Before the query, not after: a prompt sent to a CLI we cannot
            # hear is a turn that runs — tools and all — with nobody watching.
            raise StreamEnded(pump.why_ended())
        self.session.rails.new_turn()
        if self.attachments:
            images, self.attachments = list(self.attachments), []
            await client.query(_attachments.one(_attachments.user_message(line, images)))
        else:
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

        interrupts: list[asyncio.Task] = []

        def stop_now() -> None:
            self._stopping = True
            self._say(ui.yellow("\n  stopping…"))
            # Kept, and awaited in the finally below: fire-and-forget surfaced
            # a failed interrupt (the turn already over) later as "Task
            # exception was never retrieved" instead of as a line here.
            interrupts.append(asyncio.create_task(client.interrupt()))

        queue = self.pump.open_turn()
        try:
            with KeyWatcher(stop_now) as keys:
                while True:
                    message = await queue.get()
                    if isinstance(message, PumpClosed):
                        raise StreamEnded(
                            "mid-turn"
                            + (f" — {type(message.error).__name__}: {message.error}" if message.error else ""))
                    if isinstance(message, StreamEvent):
                        self._on_stream(message)
                    elif isinstance(message, SystemMessage) and message.subtype == "api_retry":
                        self._on_retry(message.data or {})
                    elif isinstance(message, ConversationResetMessage):
                        self._say(ui.green("\n  conversation cleared")
                                  + ui.dim(" — a new conversation, with its own id. `/session` shows it;"
                                           "\n  the old one is still in `scivo sessions`.\n"))
                    elif isinstance(message, TaskNotificationMessage):
                        self._on_task_note(message)
                    elif isinstance(message, TaskUpdatedMessage):
                        self._on_task_update(message)
                    elif isinstance(message, SystemMessage):
                        self._on_system(message)
                    elif isinstance(message, AssistantMessage):
                        self._on_assistant(message)
                    elif isinstance(message, UserMessage):
                        self._on_tool_result(message)
                    elif isinstance(message, ResultMessage):
                        self._on_result(message)
                    if isinstance(message, ResultMessage):
                        break
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
        except StreamEnded:
            # Not a failed turn to report and move on from — see the class.
            raise
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
            self.pump.close_turn()
            if self.control and self.control.active:
                self.control.waiting = False
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            for task in interrupts:
                try:
                    await task
                except Exception as exc:  # noqa: BLE001
                    self._say(ui.dim(f"  (interrupt: {type(exc).__name__}: {exc})"))
            self._clear_activity()
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    def _print_recap(self) -> None:
        """Show the tail of a resumed conversation, shaped as it was.

        `scivo -c` printed the same banner as a fresh session, so there was no
        way to tell whether it had picked up the conversation you meant. The
        first version of this then squashed each turn through
        `" ".join(text.split())` and cut it at 240 characters, which collapses
        every paragraph, list and code block into one dim run-on line: a
        conversation came back looking like a log file, and the cut landed
        mid-sentence (user, 2026-10-29 — "뭉쳐져서 구림").

        So each turn is printed the way the live session prints it: the user's
        at the prompt it was typed at, the reply flush-left with its own line
        breaks, and anything too long to show cut BY LINES with a count of what
        was left out.
        """
        if not self.session.resumed:
            return
        from . import sessions

        turns = sessions.recap(self.session.resumed)
        if not turns:
            print(ui.dim(f"  continuing {self.session.resumed[:8]} — no transcript to show\n"))
            return
        print(ui.dim(f"  continuing {self.session.resumed[:8]}, from where it left off:\n"))
        for role, text in turns:
            for line in recap_block(role, text):
                print(line)
            print()
        print(ui.dim("  ---- live from here ----\n"))

    async def run(self) -> None:
        briefing = self.session.briefing
        label = self.session.model
        if self.session.provider.has_endpoint:
            label += f" @ {self.session.provider.name}"
        print(ui.banner(briefing.project_name, briefing.project_id,
                        self.session.plan.summary(), label))
        for line in ui.attention(briefing):
            print(line)
        if self.session.lean_tools:
            print(ui.dim(f"  lean kit: {self.session.lean_tools} scivo tools"
                         + ("" if self.session.plan.profile != "full"
                            else " — `--profile video | deck | analysis` loads that domain too")))
        if self.session.endpoint is not None and self.session.endpoint.note:
            print(ui.dim(f"  · {self.session.endpoint.note}"))
        if self.mode == "bypassPermissions":
            # Loud, because `/update` now carries this mode across a restart:
            # a session that asks for nothing must never be a state you come
            # back into without being told.
            print(ui.red("  permissions: bypassPermissions")
                  + ui.dim(" — nothing will ask, including uploads, publishing "
                           "and remote jobs. `/dangerously-skip-permissions off`"))
        elif self.mode != "default":
            print(ui.dim(f"  permissions: {self.mode}"))
        print()
        self._print_recap()

        self.client = ClaudeSDKClient(options=self.session.options)
        await self.client.connect()
        await self._start_pump()
        if self.auto_control:
            await self._control("")
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
                    # `via == "terminal"`, not `!= "web"`: a line this process
                    # generated itself (a task wake-up) is not a path someone
                    # dropped at the prompt, and probing it sent prose to
                    # `os.stat`.
                    if command in {"/paste", "/img"} or (
                            via == "terminal" and _attachments.image_path_in(line)):
                        self._attach(command, argument.strip() if command in {"/paste", "/img"} else line)
                        continue
                    if command == "/profile":
                        if self.control and self.control.active and via != "web":
                            self.control.user(line, via)
                        await self._profile(argument.strip())
                        continue
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
                    if command == "/notify":
                        if self.control and self.control.active and via != "web":
                            self.control.user(line, via)
                        self._notify(argument.strip())
                        continue
                    if command == "/handoff":
                        if self.control and self.control.active and via != "web":
                            self.control.user(line, via)
                        # The same words the PreCompact hook sends, from one
                        # definition: two copies of an instruction drift, and
                        # the drift is invisible — one path would quietly ask
                        # for something else.
                        if not await self._guarded_turn(HANDOFF_ASK):
                            break
                        continue
                    if self.control and self.control.active and via != "web":
                        self.control.user(line, via)
                    try:
                        if self._run_local(line):
                            continue
                    except EOFError:
                        break

                    if not await self._guarded_turn(line):
                        break
        finally:
            if self.control and self.control.active:
                await self.control.stop()
            await self._stop_pump()
            if self._runs_task is not None:
                self._runs_task.cancel()
                await asyncio.gather(self._runs_task, return_exceptions=True)
            await self.client.disconnect()
            if self.session.endpoint is not None:
                self.session.endpoint.close()
        if self.session.provider.has_endpoint:
            print(ui.dim(f"session ended — {self.turns} turns on the "
                         f"{self.session.provider.name} endpoint"))
        elif _billed_to_api(self.session.provider):
            print(ui.dim(f"session total ${self.cost:.4f}"))
        else:
            print(ui.dim(f"session ended — {self.turns} turns on your claude.ai plan"))
        if self.session_id:
            print(ui.dim(f"resume: scivo resume {self.session_id[:8]}   (or scivo -c)"))
