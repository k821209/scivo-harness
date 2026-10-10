"""Rules the guide states as prose, enforced as hooks.

Each rule here corresponds to a failure the project guide documents as having
actually happened, and each is one a model can agree with and then not follow —
because the moment it matters is nine tool calls into an unrelated task. A hook
fires at that moment instead.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from claude_agent_sdk import HookMatcher

# `ssh host "nohup ... &"` — the guide's #1 reason the Runs tab is blind.
# Crosses `;`, `|` and newlines (a heredoc body): the class `[^\n|;]` let
# `ssh gpu "cd /data; nohup python train.py &"` — the most natural way to
# write it — straight through, and a bare trailing `&` was never in the
# alternation at all. `&&`, `|&`, and `2>&1` are not backgrounding.
RAW_REMOTE_JOB = re.compile(
    r"\bssh\b.*?(?:\bnohup\b|\bsetsid\b|\bdisown\b|\bscreen\s+-d\w*"
    r"|\btmux\s+new(?:-session)?\b[^\n]*?\s-d\b|(?<![&|<>])&(?![&|>]))",
    re.IGNORECASE | re.DOTALL,
)
# A remote `pgrep -f`/`pkill -f` matches the ssh command line that carries the
# pattern, so it kills the session or over-counts. Both happened on this account.
# `--full` is the long spelling of the same flag.
REMOTE_SELF_MATCH = re.compile(
    r"\bssh\b.*?\b(pgrep|pkill)\s+(?:-{1,2}[\w-]+\s+)*(?:-\w*f\w*|--full)(?=\s|$)",
    re.IGNORECASE | re.DOTALL,
)
# Host tools that write. A read after one of these may return something new,
# exactly as after a scivo write.
HOST_WRITES = ("Edit", "Write", "NotebookEdit", "MultiEdit")

# Long-running foreground work that leaves no run record.
ANALYSIS_SHAPED = re.compile(
    r"\b(python3?|Rscript|snakemake|nextflow|salmon|STAR|bwa|samtools|bcftools|plink|gatk|hisat2)\b",
    re.IGNORECASE,
)

# Hardware belongs in the servers registry, never in project memory.
COMPUTE_SPECS = [
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "an IP address"),
    (re.compile(r"\b(H100|A100|B200|GB10|RTX\s*\d{3,4}|L40S?|V100)\b", re.I), "a GPU model"),
    (re.compile(r"\b\d+\s*(?:cores?|코어|cpus?)\b", re.I), "a core count"),
    (re.compile(r"\b\d+\s*(?:GB|TB)\s*(?:RAM|메모리|memory)\b", re.I), "a memory size"),
    (re.compile(r"\bssh\s+(?:-\w+\s+)*[a-z0-9_.-]+@|~/\.ssh/config\b", re.I), "an ssh target"),
    (re.compile(r"\b(?:conda|micromamba)\s+(?:env|activate)\b|\bminiforge3?/envs/|\bminiconda3?/envs/", re.I),
     "a conda environment"),
]


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _ask(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def _arguments(payload: Any) -> str:
    """The call's arguments, serialised the same way everywhere, so the
    pre-check and the post-hooks agree on what 'the same call' means."""
    try:
        return json.dumps(payload.get("tool_input", {}), sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload.get("tool_input", {}))


def _note(context: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}}


# How many times the same call, with the same arguments, may repeat inside one
# turn before the harness stops answering it.
SAME_CALL_LIMIT = 3
SAME_CALL_STOP = 5
BASH_NUDGE = 5     # after five identical Bash calls, suggest Monitor once

# A command that FAILS the same way again has nothing to watch: that is the
# shape the per-turn counters above cannot see, because they are cleared every
# turn and because Bash is never denied. A local model spent ten hours on
# `ssh node2 'docker cp …'` every 7.8 seconds — 949 SSH connections into
# another machine on the LAN in three hours, each failing with the same
# message — and nothing stopped it (user, 2026-10-10). Failures are therefore
# counted across turns, in a time window, and a success clears the count.
FAILED_REPEAT_LIMIT = 3     # identical command, identical failure
FAILURE_WINDOW = 900.0      # seconds; older failures are forgotten
_ERROR_SIGNATURE_CHARS = 120

# Watching something change is not a loop: these are asked again on purpose,
# with the same arguments, until the thing they watch has moved.
POLLING = (
    "tail_remote_log", "poll_remote_pids", "refresh_log_tail", "server_status",
    "list_analysis_runs", "get_analysis_run", "heartbeat_run", "scan_untracked_jobs",
    "scan_recent_outputs", "youtube_check", "youtube_status",
)

# An MCP tool whose name starts with one of these only READS. Anything else
# on the scivo server writes something — and after a write, any read may
# return something new, so the repeat counters start over. The breaker used
# to compare arguments alone: `preview_slide(slug, deck, slide)` after
# `update_slide(...)` looked identical to the call before the edit and was
# blocked on the third try, which is the edit → preview → fix loop
# /paper-deck requires (Scivo feedback 635e07ec5631).
READ_PREFIXES = (
    "get_", "list_", "search_", "preview_", "lint_", "count_", "read_", "check_",
    "compare_", "verify_", "whoami", "project_guide", "tail_", "poll_", "scan_",
)


def is_scivo_write(tool_name: str) -> bool:
    if not tool_name.startswith("mcp__"):
        return False
    label = tool_name.split("__")[-1]
    return not label.startswith(READ_PREFIXES)


class Guardrails:
    """Session-scoped state for the rules that need to count."""

    def __init__(self) -> None:
        self.adhoc_runs = 0
        self.blocked: list[str] = []
        self.repeats: dict[tuple[str, str], int] = {}
        self.looping: str | None = None
        # (tool, arguments) -> [count, error signature, last time]. Survives
        # new_turn on purpose: a loop that retries once per turn is exactly
        # what the per-turn counters miss.
        self.failures: dict[tuple[str, str], list] = {}

    def new_turn(self) -> None:
        """Repetition is counted per turn: asking again next turn is fine."""
        self.repeats.clear()
        self.looping = None

    # ── repeated identical failures ──────────────────────────────────────────

    def _failure_strikes(self, key: tuple[str, str], now: float) -> int:
        rec = self.failures.get(key)
        if not rec or now - rec[2] > FAILURE_WINDOW:
            return 0
        return int(rec[0])

    def record_failure(self, name: str, arguments: str, error: str,
                       now: float | None = None) -> int:
        """Count one failure of `name`+`arguments`. Returns the new strike
        count, which only rises while the error stays the same — a command
        failing a NEW way is making progress, not looping."""
        now = time.monotonic() if now is None else now
        sig = " ".join(str(error or "").split())[:_ERROR_SIGNATURE_CHARS]
        key = (name, arguments)
        rec = self.failures.get(key)
        if rec and now - rec[2] <= FAILURE_WINDOW and rec[1] == sig:
            rec[0] += 1
            rec[2] = now
        else:
            rec = [1, sig, now]
            self.failures[key] = rec
        return int(rec[0])

    def record_success(self, name: str, arguments: str) -> None:
        """A command that worked is not looping; forget its strikes."""
        self.failures.pop((name, arguments), None)

    async def on_tool_failure(self, payload: Any, tool_use_id: str | None,
                              context: Any) -> dict[str, Any]:
        """PostToolUseFailure: remember what failed and how."""
        if payload.get("is_interrupt"):
            return {}          # the user stopped it; that is not a loop
        name = str(payload.get("tool_name", ""))
        self.record_failure(name, _arguments(payload), str(payload.get("error", "")))
        return {}

    async def on_tool_success(self, payload: Any, tool_use_id: str | None,
                              context: Any) -> dict[str, Any]:
        """PostToolUse: a result arrived, so this call is not stuck."""
        self.record_success(str(payload.get("tool_name", "")), _arguments(payload))
        return {}

    async def on_any_tool(self, payload: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        """Break a tool-call loop.

        The rule fires on MCP tools: a local model that answered "확인" by
        calling list_todos and list_papers, reading both results, and then
        calling them again — twenty times — is stuck; nothing in that loop
        changes and the results were delivered every time.

        Bash is different. A `ssh … tail log` run five times in a row is the
        model watching a long-running job, not a stuck loop; the log's tail
        changes between calls. So Bash never denies — it notes on the fifth
        identical run that `Monitor` with an until-loop is the pattern for
        that, and stays out of the way.
        """
        name = str(payload.get("tool_name", ""))
        arguments = _arguments(payload)
        key = (name, arguments)
        # Checked before the polling exemption: `tail_remote_log` is asked
        # again on purpose while a job runs, but not after it has failed the
        # same way three times — that is an unreachable host, not a job to
        # watch.
        strikes = self._failure_strikes(key, time.monotonic())
        if strikes >= FAILED_REPEAT_LIMIT:
            label = name.split("__")[-1] if name.startswith("mcp__") else name
            self.looping = label
            return _deny(
                f"Blocked: this exact `{label}` call has already failed {strikes} times with the "
                f"same error — {self.failures[key][1]}\n"
                "Repeating it cannot change the outcome. Read the error, fix the command or the "
                "thing it depends on, or tell the user what is wrong and stop."
            )
        if any(marker in name for marker in POLLING):
            return {}
        if name in HOST_WRITES:
            self.repeats.clear()
            return {}
        if not name.startswith("mcp__"):
            # Host tools are never denied. The rule is about MCP calls; a
            # third identical `Read` (read → edit → read → edit → read) or a
            # `Monitor` with the same until-loop — the very thing the Bash
            # nudge below recommends — used to be denied, and at five the turn
            # was interrupted.
            if name == "Bash":
                seen = self.repeats[key] = self.repeats.get(key, 0) + 1
                if seen == BASH_NUDGE:
                    return _note(
                        "This Bash command has run identically several times. If it is polling for a "
                        "log line or a file to appear, use `Monitor` with an until-loop instead — that "
                        "backs off, times out cleanly, and does not spend a turn per check."
                    )
            return {}
        if is_scivo_write(name):
            # A write may change what every read returns: forget the reads
            # counted so far (this call's own count stays, so the same write
            # repeated verbatim is still a loop).
            self.repeats = {key: self.repeats.get(key, 0)}
        seen = self.repeats[key] = self.repeats.get(key, 0) + 1
        if seen < SAME_CALL_LIMIT:
            return {}
        label = name.split("__")[-1]
        if seen >= SAME_CALL_STOP:
            self.looping = label
        return _deny(
            f"Blocked: `{label}` has already run {seen - 1} times in this turn with exactly "
            "these arguments, and returned each time. Calling it again cannot produce anything "
            "new.\n"
            "Use the result you already have and answer the user. If it genuinely did not answer "
            "the question, say that in words instead of repeating the call."
        )

    async def on_bash(self, payload: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        command = str(payload.get("tool_input", {}).get("command", ""))

        if RAW_REMOTE_JOB.search(command):
            self.blocked.append("raw remote job")
            return _deny(
                "Blocked: a remote job launched through raw ssh leaves no run record, so the "
                "Runs tab cannot say which machine, command and parameters produced the result.\n"
                "Use `mcp__scivo__submit_remote_job(...)` instead — it creates the record "
                "(host, command, env_name, log_path, pid) as it launches.\n"
                "If this really is not a result-producing analysis (a one-off copy, a cleanup), "
                "say so to the user and run it in the foreground."
            )

        if REMOTE_SELF_MATCH.search(command):
            self.blocked.append("remote pgrep/pkill -f")
            return _deny(
                "Blocked: over ssh, `pgrep -f`/`pkill -f` matches the ssh command line that "
                "carries the pattern. `pkill` then kills your own session and `pgrep -c` "
                "over-counts, so a finished job reads as still running.\n"
                "Count with a bracketed pattern: ssh <host> \"ps -eo args | grep '[w]get'\"\n"
                "Kill through the PID: ssh <host> \"kill \\$(pgrep -f <pattern>)\""
            )

        if ANALYSIS_SHAPED.search(command):
            self.adhoc_runs += 1
            if self.adhoc_runs == 2:
                return _note(
                    "This is the second analysis-shaped command this session with no run record. "
                    "The guide names exactly this point as where provenance is lost — not the big "
                    "jobs, the exploratory stretch. Before the next one, create the analysis and "
                    "record these runs (`create_analysis` + `record_analysis_run` with host, "
                    "command, env_name, log_path and `params=`), or switch to `/analysis-run`."
                )
        return {}

    async def on_memory_write(self, payload: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool_input = payload.get("tool_input", {})
        text = str(tool_input.get("note") or tool_input.get("content")
                   or tool_input.get("skills") or tool_input.get("text") or "")
        hits = [label for pattern, label in COMPUTE_SPECS if pattern.search(text)]
        if not hits:
            return {}
        # A deny, not an ask: bypassPermissions is reachable mid-session and
        # erases an ask, while the ssh denies still hold. The redirect says
        # what to write instead; a memory note that merely mentions a host can
        # be rewritten without the machine facts.
        return _deny(
            f"Blocked: this memory write contains {', '.join(sorted(set(hits)))}. Compute belongs in "
            "the servers registry (`add_server` / `add_server_env` / `update_server`), which drives "
            "the Runs tab and the politeness caps; project memory and the project playbook are for "
            "soft knowledge only. The guide calls this the single most common mistake.\n"
            "Register the machine instead, and write the note again with the machine facts left out."
        )


def build(rails: Guardrails) -> dict[str, list[HookMatcher]]:
    return {
        "PreToolUse": [
            # Monitor runs shell too — it is what the Bash nudge recommends for
            # polling — so the same rules read its command.
            HookMatcher(matcher="Bash|Monitor", hooks=[rails.on_bash]),
            HookMatcher(
                matcher="mcp__scivo__append_project_memory|mcp__scivo__update_project_memory"
                        "|mcp__scivo__update_project_skills",
                hooks=[rails.on_memory_write],
            ),
            HookMatcher(hooks=[rails.on_any_tool]),   # no matcher: every tool
        ],
        # A failing command is only visible after it runs, and the loop this
        # catches repeats once per turn — so the count has to live outside the
        # per-turn counters above, fed by what actually happened.
        "PostToolUseFailure": [HookMatcher(hooks=[rails.on_tool_failure])],
        "PostToolUse": [HookMatcher(hooks=[rails.on_tool_success])],
    }
