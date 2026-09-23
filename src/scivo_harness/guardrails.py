"""Rules the guide states as prose, enforced as hooks.

Each rule here corresponds to a failure the project guide documents as having
actually happened, and each is one a model can agree with and then not follow —
because the moment it matters is nine tool calls into an unrelated task. A hook
fires at that moment instead.
"""

from __future__ import annotations

import re
import json
from typing import Any

from claude_agent_sdk import HookMatcher

# `ssh host "nohup ... &"` — the guide's #1 reason the Runs tab is blind.
RAW_REMOTE_JOB = re.compile(
    r"\bssh\b[^\n|;]*?\b(?:nohup|setsid|disown|screen\s+-d|tmux\s+new-session\s+-d)\b",
    re.IGNORECASE,
)
# A remote `pgrep -f`/`pkill -f` matches the ssh command line that carries the
# pattern, so it kills the session or over-counts. Both happened on this account.
REMOTE_SELF_MATCH = re.compile(r"\bssh\b[^\n]*\b(pgrep|pkill)\s+(-\w+\s+)*-\w*f\w*\s", re.IGNORECASE)

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


def _note(context: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}}


# How many times the same call, with the same arguments, may repeat inside one
# turn before the harness stops answering it.
SAME_CALL_LIMIT = 3
SAME_CALL_STOP = 5
BASH_NUDGE = 5     # after five identical Bash calls, suggest Monitor once

# Watching something change is not a loop: these are asked again on purpose,
# with the same arguments, until the thing they watch has moved.
POLLING = (
    "tail_remote_log", "poll_remote_pids", "refresh_log_tail", "server_status",
    "list_analysis_runs", "get_analysis_run", "heartbeat_run", "scan_untracked_jobs",
    "scan_recent_outputs", "youtube_check", "youtube_status",
)


class Guardrails:
    """Session-scoped state for the rules that need to count."""

    def __init__(self) -> None:
        self.adhoc_runs = 0
        self.blocked: list[str] = []
        self.repeats: dict[tuple[str, str], int] = {}
        self.looping: str | None = None

    def new_turn(self) -> None:
        """Repetition is counted per turn: asking again next turn is fine."""
        self.repeats.clear()
        self.looping = None

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
        if any(marker in name for marker in POLLING):
            return {}
        try:
            arguments = json.dumps(payload.get("tool_input", {}), sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            arguments = str(payload.get("tool_input", {}))
        key = (name, arguments)
        seen = self.repeats[key] = self.repeats.get(key, 0) + 1
        if name == "Bash":
            if seen == BASH_NUDGE:
                return _note(
                    "This Bash command has run identically several times. If it is polling for a "
                    "log line or a file to appear, use `Monitor` with an until-loop instead — that "
                    "backs off, times out cleanly, and does not spend a turn per check."
                )
            return {}
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
        text = str(tool_input.get("note") or tool_input.get("content") or "")
        hits = [label for pattern, label in COMPUTE_SPECS if pattern.search(text)]
        if not hits:
            return {}
        return _ask(
            f"This memory write contains {', '.join(sorted(set(hits)))}. Compute belongs in the "
            "servers registry (`add_server` / `add_server_env` / `update_server`), which drives "
            "the Runs tab and the politeness caps; project memory is for soft knowledge only. "
            "The guide calls this the single most common mistake.\n"
            "Register the machine instead, and keep in memory only what is not a machine fact. "
            "Approve this only if the text really is soft knowledge that merely mentions a host."
        )


def build(rails: Guardrails) -> dict[str, list[HookMatcher]]:
    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[rails.on_bash]),
            HookMatcher(
                matcher="mcp__scivo__append_project_memory|mcp__scivo__update_project_memory",
                hooks=[rails.on_memory_write],
            ),
            HookMatcher(hooks=[rails.on_any_tool]),   # no matcher: every tool
        ]
    }
