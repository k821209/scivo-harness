"""Rules the guide states as prose, enforced as hooks.

Each rule here corresponds to a failure the project guide documents as having
actually happened, and each is one a model can agree with and then not follow —
because the moment it matters is nine tool calls into an unrelated task. A hook
fires at that moment instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

from claude_agent_sdk import HookMatcher

from . import ui
from .prompt import HANDOFF_ASK

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
# A heredoc body is DATA the command writes, not a command it runs. Scanning it
# blocked `cat > notes.rs <<'EOF' … ssh … EOF` — a local file write whose Korean
# comment happened to mention ssh — and the way round it was to write the same
# file with the Write tool, which improves provenance not at all (feedback
# 504eb4a8d21e). Any project whose prose discusses remote work steps on this.
# Quoted strings are NOT stripped: `ssh gpu "nohup train &"` keeps the part
# worth catching inside the quotes.
_HEREDOC = re.compile(
    r"""<<-?\s*(['"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\1   # <<EOF / <<'EOF' / <<-EOF
        (?P<body>.*?)                                     # everything up to …
        ^[ \t]*(?P=tag)[ \t]*$                            # … the closing tag on its own line
    """,
    re.DOTALL | re.MULTILINE | re.VERBOSE,
)


# Where one command ends and the next begins, so the head of the heredoc's own
# command can be read.
_SEPARATOR = re.compile(r"(?:\n|;|\|\||&&|\|)")


def executable_text(command: str) -> str:
    """The command with heredoc bodies removed, for the pattern checks.

    A body is removed only when it is DATA. `ssh host bash <<EOF … EOF` feeds
    its body to a remote shell, so there the body IS the command and stripping
    it would hide the very thing the guard exists for — which is what the first
    attempt at this did, caught by the suite.
    """
    def keep_or_drop(match: re.Match) -> str:
        head = command[:match.start()]
        last = _SEPARATOR.split(head)[-1] if head else ""
        if re.search(r"\bssh\b", last):
            return match.group(0)          # the body is the remote command
        return "<<" + match.group("tag") + "\n" + match.group("tag")

    return _HEREDOC.sub(keep_or_drop, command)


# A remote `pgrep -f`/`pkill -f` matches the ssh command line that carries the
# pattern, so it kills the session or over-counts. Both happened on this account.
# `--full` is the long spelling of the same flag.
REMOTE_SELF_MATCH = re.compile(
    r"\bssh\b.*?\b(pgrep|pkill)\s+(?:-{1,2}[\w-]+\s+)*(?:-\w*f\w*|--full)(?=\s|$)",
    re.IGNORECASE | re.DOTALL,
)
# The LOCAL form of the same mistake, and the one that actually happened. The
# guard above requires the remote prefix, so a pattern kill on THIS machine
# went straight through — and that command sends SIGTERM by default, which is
# exit 143, which is how a session died with "Cannot write to terminated
# process" (user, 2026-11-09, asking whether the model had done it to itself).
# The reasoning in the remote comment applies more directly here: the pattern
# matches the session's OWN command lines, so the kill lands on the thing
# issuing it.
LOCAL_PATTERN_KILL = re.compile(
    r"\b(pkill|killall)\s+(?P<args>[^|;&\n]*)", re.IGNORECASE)


def _own_cmdlines() -> list[str]:
    """This session's own command lines: the harness, the CLI, the MCP.

    Read from /proc rather than compared with names somebody guessed, so the
    question asked is the real one — would this pattern hit something of
    ours? — and a rename upstream cannot quietly make the guard useless.
    Empty off Linux, where the caller falls back to a narrower rule.
    """
    import os
    import pathlib

    try:
        mine = {os.getpid()}
        # Our descendants: the CLI and the MCP are children of the harness.
        for _ in range(3):           # bounded: parent, child, grandchild
            for entry in pathlib.Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    stat = (entry / "stat").read_text()
                    parent = int(stat.rsplit(")", 1)[1].split()[1])
                except (OSError, IndexError, ValueError):
                    continue
                if parent in mine:
                    mine.add(int(entry.name))
        out = []
        for pid in mine:
            try:
                raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                continue
            argv = [a for a in raw.decode("utf-8", "replace").split("\0") if a]
            program = _program_of(argv)
            if program:
                out.append(program)
        return [c for c in out if c]
    except OSError:
        return []


def _program_of(argv: list[str]) -> str:
    """The program and at most two arguments — NOT the whole command line.

    The CLI is started with `--append-system-prompt <the whole system
    prompt>`: tens of thousands of characters containing words like "python",
    "analysis" and "train". Comparing a user's pattern against that would
    block almost every legitimate kill while claiming to protect the session.
    """
    return " ".join(argv[:3])


# Shorter than this is not a pattern, it is a coincidence: "is" matched
# `co_scientist_local` and turned `echo pkill is a command` into a refusal.
MIN_PATTERN = 3


# When /proc is not there to ask, these are the words that cannot be a
# pattern: each appears in a command line this session depends on.
LETHAL_WORDS = ("claude", "scivo", "co_scientist")


def kills_this_session(command: str) -> str | None:
    """The process of ours such a pattern would hit, or None."""
    for match in LOCAL_PATTERN_KILL.finditer(command):
        words = [w for w in match.group("args").split() if not w.startswith("-")]
        if not words:
            continue
        needle = words[0].strip("\"\'")
        if len(needle) < MIN_PATTERN:
            continue
        ours = _own_cmdlines()
        for line in ours:
            if needle in line:
                return line[:120]
        if not ours:
            for word in LETHAL_WORDS:
                if word in needle.lower():
                    return f"a process matching {needle!r}"
    return None


# An argument that says where a tool should WRITE a file on this machine.
# Matched by shape rather than listed by tool: `get_asset`, `get_material`,
# `get_export` and their cousins are named like reads, so they are approved
# without a prompt — and each takes a destination the MODEL chooses, expands
# `~`, and overwrites what is there. Nothing scoped them to the project the
# way Write is scoped: a "read-only" call could replace `~/.bashrc` with the
# bytes of a project file, unasked (review 2026-11-10). A tool added tomorrow
# with a `dest_path` is covered the day it ships.
DESTINATION_ARG = re.compile(r"^(?:dest|out|output|save|target)(?:_[a-z]+)*_(?:path|dir|file)$|^dest$")


# Host tools that write. A read after one of these may return something new,
# exactly as after a scivo write.
HOST_WRITES = ("Edit", "Write", "NotebookEdit", "MultiEdit")

# Long-running foreground work that leaves no run record.
ANALYSIS_PROGRAMS = frozenset({
    "python", "python3", "rscript", "snakemake", "nextflow", "salmon", "star", "bwa",
    "samtools", "bcftools", "plink", "gatk", "hisat2",
})
# The same words as a pattern, for a line that cannot be read as commands.
ANALYSIS_SHAPED = re.compile(
    r"\b(python3?|Rscript|snakemake|nextflow|salmon|STAR|bwa|samtools|bcftools|plink|gatk|hisat2)\b",
    re.IGNORECASE,
)
# `python -m <these>` is tooling, not a computation behind a number.
_NOT_ANALYSIS_MODULES = frozenset({
    "pytest", "pip", "pyflakes", "venv", "ensurepip", "build", "unittest", "compileall",
    "json.tool", "http.server", "scivo_harness", "co_scientist_local", "ruff", "mypy", "black",
})


def analysis_shaped(command: str, _depth: int = 0) -> bool:
    """Does this line RUN one of the programs that produce results?

    It used to be the pattern alone, on the whole text — so it fired on a
    `sed` whose path ran through `…/lib/python3.11/site-packages/…`, on
    `pytest`, on `grep -rn plink docs/`. The nudge it feeds is given ONCE, at
    the second such command; spent on a `sed`, it was not there for the
    second real analysis (review 2026-11-10 — it fired on the reviewer).

    So the question is asked of the PROGRAM of each command on the line, and
    of what an `ssh` is told to run on the other side. A line that cannot be
    read as commands falls back to the pattern: over-counting there costs a
    reminder, and under-counting costs a result with no provenance.
    """
    from .shellread import shell_parts, ssh_target

    parts = shell_parts(command)
    if parts is None:
        return bool(ANALYSIS_SHAPED.search(command))
    for argv in parts:
        program = argv[0].rsplit("/", 1)[-1].lower()
        rest = argv[1:]
        if program == "ssh" and _depth == 0:
            _host, remote = ssh_target(rest)
            if remote and analysis_shaped(" ".join(remote), _depth=1):
                return True
            continue
        # python3.11, python3.12 — the interpreter, whatever its minor version
        base = re.sub(r"^(python)[0-9.]*$", r"\1", program)
        if base not in ANALYSIS_PROGRAMS:
            continue
        if base == "python":
            if not rest or rest[0] in ("-V", "--version", "-h", "--help"):
                continue
            if rest[0] == "-m" and len(rest) > 1 and rest[1] in _NOT_ANALYSIS_MODULES:
                continue
        return True
    return False


# An Enzyme Commission number is four dot-separated numbers — EC 3.2.1.35 —
# so a bare dotted-quad regex calls it an IP address and denies the write. It
# did: a venom-enzyme census could not record its own Table 2, and the block
# said "this memory write contains an IP address" about a note that mentioned
# no machine at all, which sent the author looking for an IP that was never
# there. The way around it was to break the notation (3-2-1-35), i.e. to make
# the memory less accurate to satisfy a guard that had nothing to guard
# (reported 2026-10-15).
_DOTTED_QUAD = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w.])")
# "EC 3.2.1.35", "EC:3.2.1.35", "(EC 3.2.1.35)" — the marker sits just before.
_EC_MARKED = re.compile(r"\bEC[\s:]*$", re.I)
# EC classes run 1-7, so a first segment in that range inside a note that is
# talking about enzymes is an EC number, not an address. This catches the
# second and later numbers in a list, where only the first carries the marker.
_ENZYME_CONTEXT = re.compile(r"\bEC[\s:]*\d|\benzymes?\b|\bEC numbers?\b|효소", re.I)


def _mentions_ip(text: str) -> bool:
    """True when `text` holds something that is really an address.

    A miss costs a machine fact landing in project memory, which the author
    can move. A false positive costs a refused write with a diagnosis that
    sends them hunting for something that is not in their note — so when the
    evidence says enzyme, this yields.
    """
    for match in _DOTTED_QUAD.finditer(text):
        octets = [int(group) for group in match.groups()]
        if any(octet > 255 for octet in octets):
            continue                                    # not an address at all
        if _EC_MARKED.search(text[:match.start()]):
            continue
        if octets[0] <= 7 and _ENZYME_CONTEXT.search(text):
            continue
        return True
    return False


# Hardware belongs in the servers registry, never in project memory.
COMPUTE_SPECS = [
    (_mentions_ip, "an IP address"),
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


# The marks that say "this backgrounded ssh is not a job" — the same three the
# hook file for plain Claude Code has always read (pretool_block_ssh_nohup.py),
# so one command means one thing on every host.
SETUP_PREFIXES = ("# setup", "# manual")
SETUP_INLINE = "# allow-untracked"


def marked_setup(command: str) -> bool:
    return (command.lstrip().startswith(SETUP_PREFIXES)) or SETUP_INLINE in command


def _arguments(payload: Any) -> str:
    """The call's arguments, serialised the same way everywhere, so the
    pre-check and the post-hooks agree on what 'the same call' means."""
    try:
        return json.dumps(payload.get("tool_input", {}), sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload.get("tool_input", {}))


def _key(payload: Any) -> tuple[str, str]:
    """What "the same call" means, for every counter here.

    For Bash it is the command alone. The `description` beside it is prose the
    model writes fresh each time, so a loop that reworded it was a new call on
    every pass and no counter ever moved.
    """
    name = str(payload.get("tool_name", ""))
    if name == "Bash":
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if isinstance(command, str):
            return name, command.strip()
    return name, _arguments(payload)


def _result_text(payload: Any) -> str | None:
    """What the tool returned, as text — None when the payload does not carry
    it, which is "cannot tell", not "returned nothing"."""
    if "tool_response" not in payload:
        return None
    response = payload.get("tool_response")
    if response is None:
        return None
    if isinstance(response, str):
        return response
    if isinstance(response, dict) and ("stdout" in response or "stderr" in response):
        out, err = str(response.get("stdout") or ""), str(response.get("stderr") or "")
        return (out + ("\n" + err if err else "")).strip()
    if isinstance(response, list) and response and all(
            isinstance(block, dict) and isinstance(block.get("text"), str) for block in response):
        return "\n".join(block["text"] for block in response)
    try:
        return json.dumps(response, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(response)


def _excerpt(text: str) -> str:
    if len(text) <= RESULT_EXCERPT:
        return text
    return (text[:RESULT_EXCERPT]
            + f"\n… (the first {RESULT_EXCERPT:,} of {len(text):,} characters)")


def _note(context: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}}


# How many times the same call, with the same arguments, may repeat inside one
# turn before the harness stops answering it.
SCIVO_PREFIX = "mcp__scivo__"
SAME_CALL_LIMIT = 3
SAME_CALL_STOP = 5
BASH_NUDGE = 5     # after five identical Bash calls, suggest Monitor once

# Bash is never refused for being asked again — watching a log is asking again
# on purpose — but that rests on the output CHANGING between calls. A command
# that has printed byte-for-byte the same thing this many times in one turn,
# with nothing new run in between, is not watching anything.
BASH_SAME_OUTPUT = 3

# Refusals add up across calls. Counting each call by itself let a model step
# around the refusal: A, A, A (refused), B, B, B (refused), C, C, C (refused),
# and round again — fifteen refusals in forty-five calls and the turn never
# ended, because no one call reached SAME_CALL_STOP (feedback f5c4b6108ef6: a
# local model collecting a three-part briefing, stopped by hand).
REFUSALS_STOP = 3

# A call that comes back refused WITHOUT having run — by a guard here, by a
# hook file, by the CLI ("No such tool available") — and is made again. Nothing
# counted these: a refused call has no result and no failure, so every counter
# above stood still while one turn made three commands 1,114 times in five
# hours, each refused, through three compactions (msja, 2026-11-12). The same
# refusal of the same call counts as the same failure (FAILED_REPEAT_LIMIT),
# and this many refusals of anything in one turn ends it: by then the model is
# looking for a way round the rule, not for what the rule asks.
UNRUN_STOP = 8

# The same call returning the same thing, counted over the WHOLE turn. The
# rule above forgets its count when a command not yet run this turn appears,
# so three polls, an `ls`, three polls, an `md5sum` went on for ever (msja,
# 2026-11-13: 41 of 59 calls the same history query). This one is not reset
# by anything but the result changing or a file being edited — if the fifth
# answer is the first answer, whatever ran in between did not matter to it.
# Bash and other servers' tools only: our own `preview_slide` returns the same
# path for a slide that has changed. And only in a session that has a turn
# budget, which is to say for the models that loop: a test run six times with
# the same failure after five fixes made through the shell is slow work, not
# a loop, and a model that does that work is not refused for it.
SAME_RESULT_TOTAL = 5

# A turn budget for a model that does not stop by itself (providers.py,
# `turn_budget`). The model is told at three quarters; at the limit further
# calls are refused, and the turn is ended only if it goes on calling.
BUDGET_WARN_AT = 0.75
BUDGET_OVERRUN = 3
# Asking the person is the thing the budget is there to produce.
NEVER_BUDGETED = ("AskUserQuestion",)

# How much of the result a refusal hands back. The model that loops is often
# the one that did not take the result in; being told "you have it" does not
# give it to them, and running the tool again is the only other way to get it.
RESULT_EXCERPT = 1200

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
def is_scivo_write(tool_name: str) -> bool:
    """Anything on an MCP server that is not, by its name, a read.

    "By its name" is `toolsets.is_read_only` — the same test that decides
    which tools run without a prompt. There were two lists, and they had
    drifted: this one lacked `diff_`, `validate_` and `server_status`, so
    each of those counted as a WRITE here and wiped the repeat counters,
    and a loop that happened to include one could never trip the breaker.
    """
    if not tool_name.startswith("mcp__"):
        return False
    from .toolsets import is_read_only

    return not is_read_only(tool_name.split("__")[-1])


class Guardrails:
    """Session-scoped state for the rules that need to count."""

    def __init__(self, root: Any = None, extra_roots: Any = ()) -> None:
        # Where this session may write without being asked: the project, and
        # the directories the USER added on the command line.
        self.roots = self._resolved([root, *list(extra_roots or ())]) if root else []
        # For the usage tally only (which project a skill was invoked in);
        # set by `session.build` once the briefing knows it.
        self.project_id = ""
        self.adhoc_runs = 0
        self.blocked: list[str] = []
        self.repeats: dict[tuple[str, str], int] = {}
        self.looping: str | None = None
        # Why the turn is being ended, in words for the person watching.
        self.stop_reason = ""
        # Every call made this turn. A write that was already made, the same
        # way, has changed nothing the second time.
        self.calls: set[tuple[str, str]] = set()
        # Bash only: command -> [output signature, times in a row]. And for
        # every tool, what it last returned, to hand back with a refusal.
        self.outputs: dict[tuple[str, str], list] = {}
        self.returned: dict[tuple[str, str], str] = {}
        self.refusals = 0
        # Calls refused before they ran, this turn; and the ids of the ones
        # the loop breaker refused itself, which it has already counted.
        self.unrun = 0
        self.counted: set[str] = set()
        # What to tell the person when the turn is ended.
        self.stop_hint = ""
        # (tool, call) -> [result signature, times it has come back the same
        # this turn]; never cleared by other calls.
        self.same_result: dict[tuple[str, str], list] = {}
        # Tool calls this turn, and how many one turn may make (None: no
        # limit). Set by the session from the provider.
        self.turn_calls = 0
        self.turn_budget: int | None = None
        # The scivo tools this session carries, bare names — None until the
        # session says. A refusal that sends the model to a tool checks here
        # first.
        self.loaded: set[str] | None = None
        # (tool, arguments) -> [count, error signature, last time]. Survives
        # new_turn on purpose: a loop that retries once per turn is exactly
        # what the per-turn counters miss.
        self.failures: dict[tuple[str, str], list] = {}

    @staticmethod
    def _resolved(paths: list) -> list:
        import pathlib

        out = []
        for path in paths:
            try:
                out.append(pathlib.Path(str(path)).expanduser().resolve())
            except (OSError, ValueError):
                continue
        return out

    def outside_project(self, tool_input: dict) -> list[tuple[str, str]]:
        """(argument, resolved path) for each destination that leaves the
        project. Relative paths are the project's own — that is the MCP
        server's working directory — so only an absolute, `~` or `..` path
        can be outside."""
        import pathlib

        if not self.roots:
            return []
        found = []
        for key, value in (tool_input or {}).items():
            if not isinstance(value, str) or not value or not DESTINATION_ARG.match(key):
                continue
            try:
                path = pathlib.Path(value).expanduser()
                if not path.is_absolute():
                    path = self.roots[0] / path
                path = path.resolve()
            except (OSError, ValueError):
                found.append((key, value))       # cannot be placed: not "inside"
                continue
            inside = False
            for root in self.roots:
                try:
                    path.relative_to(root)
                    inside = True
                    break
                except ValueError:
                    continue
            if not inside:
                found.append((key, str(path)))
        return found

    async def on_destination(self, payload: Any, tool_use_id: str | None,
                             context: Any) -> dict[str, Any]:
        """Hold a tool that would write a file outside the project.

        An ask, not a deny: saving an export to `~/Downloads` is a reasonable
        thing to want, and the person is right there. What must not happen is
        it happening unasked because the tool's name starts with `get_`.
        """
        name = str(payload.get("tool_name", ""))
        if not name.startswith("mcp__"):
            return {}               # the host's own file tools are scoped by the host
        outside = self.outside_project(payload.get("tool_input") or {})
        if not outside:
            return {}
        label = name.split("__")[-1]
        where = "; ".join(f"{key} = {path}" for key, path in outside)
        self.blocked.append(f"held: {label} writing outside the project")
        return _ask(
            f"`{label}` would write a file OUTSIDE this project ({where}). "
            f"The project is {self.roots[0]}. It overwrites whatever is at that path. "
            "Inside the project, or a directory added with --add-dir, it is not asked about."
        )

    def new_turn(self) -> None:
        """Repetition is counted per turn: asking again next turn is fine."""
        self.repeats.clear()
        self.looping = None
        self.stop_reason = ""
        self.calls.clear()
        self.outputs.clear()
        self.returned.clear()
        self.refusals = 0
        self.unrun = 0
        self.counted.clear()
        self.stop_hint = ""
        self.same_result.clear()
        self.turn_calls = 0

    def _over_budget(self, name: str, tool_use_id: str | None) -> dict[str, Any] | None:
        """Count one call against the turn's budget. Returns what to answer
        when the call may not simply go ahead, or None."""
        if not self.turn_budget or name in NEVER_BUDGETED:
            return None
        self.turn_calls += 1
        budget = self.turn_budget
        if self.turn_calls == max(1, int(budget * BUDGET_WARN_AT)):
            return _note(
                f"This turn has made {self.turn_calls} tool calls and ends at {budget}. Stop "
                "gathering: tell the user what you have found, what is still missing and what "
                "you would do next — or finish, if one more step does it. If you do not know "
                "what the user wants, ask them now.")
        if self.turn_calls <= budget:
            return None
        if tool_use_id:
            self.counted.add(tool_use_id)
        over = self.turn_calls - budget
        if over >= BUDGET_OVERRUN and not self.looping:
            self.looping = name.split("__")[-1] if name.startswith("mcp__") else name
            self.stop_reason = f"{budget} tool calls in one turn — the limit for this model"
            self.stop_hint = ("What it had found is in the conversation. Tell it what to do "
                              "next, or say `계속` to let it go on. The limit is `turn_calls` "
                              "in this provider's settings (0 turns it off).")
        return _deny(
            f"Blocked: this turn has made {budget} tool calls, which is the limit for one turn "
            "on this model. No more tools will run in this turn — this is not an error to "
            "retry.\nWrite to the user now: what you found, what is still missing, and what "
            "you would do next. They can tell you to continue."
            + (" The turn is being ended." if over >= BUDGET_OVERRUN else ""))

    def kit_changed(self, loaded: set[str]) -> None:
        """The session carries a different set of tools now (a profile or a
        model switch). What could not work a minute ago may work now, so a
        call that was refused is not held against the new kit."""
        self.loaded = set(loaded)
        self.failures.clear()

    def note_refused(self, tool_use_id: str | None, name: str, tool_input: Any,
                     text: str) -> None:
        """A call came back refused without having run. Called by the session
        for every such result, whoever refused it."""
        if tool_use_id and tool_use_id in self.counted:
            return          # the loop breaker's own refusal, counted where it was made
        key = _key({"tool_name": name, "tool_input": tool_input})
        strikes = self.record_failure(*key, text)
        self.unrun += 1
        if self.looping:
            return
        label = name.split("__")[-1] if name.startswith("mcp__") else name
        why = self.failures[key][1]
        hint = ("Nothing was run. The refusal says what it wants instead; if the model "
                "cannot get there, do that step yourself or tell it how.")
        if strikes > FAILED_REPEAT_LIMIT:
            self.looping = label
            self.stop_reason = (f"the same {label} call was refused {strikes} times and "
                                f"made again — {why}")
            self.stop_hint = hint
        elif self.unrun >= UNRUN_STOP:
            self.looping = label
            self.stop_reason = (f"{self.unrun} calls were refused in this turn before they "
                                f"ran (last: {label} — {why})")
            self.stop_hint = hint

    def _refuse(self, label: str, said: str, key: tuple[str, str],
                tool_use_id: str | None = None) -> dict[str, Any]:
        """Refuse a repeat, hand back what it returned, and end the turn once
        refusing has not worked."""
        if tool_use_id:
            self.counted.add(tool_use_id)
        self.refusals += 1
        left = REFUSALS_STOP - self.refusals
        if left <= 0 and not self.looping:
            self.looping = label
            self.stop_reason = (f"{self.refusals} repeated calls were refused in this turn "
                                f"and the model kept repeating (last: {label})")
        had = self.returned.get(key)
        parts = [said]
        if had is not None:
            parts.append("This is what it returned — you already have it:\n"
                         + (_excerpt(had) if had.strip() else "(nothing: the output was empty)"))
        parts.append(
            "This is not a temporary error and it is not a failure of the tool: trying again, "
            "or the same thing reworded, is refused the same way. Write your answer from the "
            "result above. If it does not answer the question, say so in words."
            + (f" {left} more refused call{'s' if left != 1 else ''} ends this turn."
               if left > 0 else " The turn is being ended now."))
        return _deny("\n".join(parts))

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
        self.record_failure(*_key(payload), str(payload.get("error", "")))
        return {}

    async def on_tool_success(self, payload: Any, tool_use_id: str | None,
                              context: Any) -> dict[str, Any]:
        """PostToolUse: a result arrived, so this call is not stuck."""
        name = str(payload.get("tool_name", ""))
        key = _key(payload)
        self.record_success(*key)
        text = _result_text(payload)
        if text is not None:
            self.returned[key] = text
            if name == "Bash" or (name.startswith("mcp__") and not name.startswith(SCIVO_PREFIX)):
                mark = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
                seen_all = self.same_result.get(key)
                if seen_all and seen_all[0] == mark:
                    seen_all[1] += 1
                else:
                    self.same_result[key] = [mark, 1]
            if name == "Bash":
                signature = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
                seen = self.outputs.get(key)
                if seen and seen[0] == signature:
                    seen[1] += 1
                else:
                    self.outputs[key] = [signature, 1]
        if name == "Skill":
            # The one place a model-invoked skill is visible to anything that
            # can count it (see usage.py).
            from . import usage
            tool_input = payload.get("tool_input") or {}
            usage.record_skill(str(tool_input.get("skill") or ""),
                               getattr(self, "project_id", ""))
        return {}

    async def on_any_tool(self, payload: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        """Break a tool-call loop.

        The rule fires on MCP tools: a local model that answered "확인" by
        calling list_todos and list_papers, reading both results, and then
        calling them again — twenty times — is stuck; nothing in that loop
        changes and the results were delivered every time.

        Bash is different. A `ssh … tail log` run five times in a row is the
        model watching a long-running job, not a stuck loop; the log's tail
        changes between calls. So Bash is not refused for being asked again —
        it is noted on the fifth identical run that `Monitor` with an
        until-loop is the pattern for that. It IS refused once the same
        command has printed the same output BASH_SAME_OUTPUT times with
        nothing new run in between: then nothing is being watched.

        Refusals are counted for the turn, whatever was refused; see
        REFUSALS_STOP.
        """
        name = str(payload.get("tool_name", ""))
        key = _key(payload)
        budget = self._over_budget(name, tool_use_id)
        if budget is not None and "permissionDecision" in budget.get("hookSpecificOutput", {}):
            return budget
        verdict = self._judge(payload, tool_use_id, name, key)
        # The three-quarters note rides on a call that is otherwise fine; a
        # refusal says more and wins.
        return verdict or budget or {}

    def _judge(self, payload: Any, tool_use_id: str | None, name: str,
               key: tuple[str, str]) -> dict[str, Any]:
        first = key not in self.calls
        self.calls.add(key)
        again = self.same_result.get(key) if self.turn_budget else None
        if again and again[1] >= SAME_RESULT_TOTAL:
            label = name.split("__")[-1] if name.startswith("mcp__") else name
            return self._refuse(
                label,
                f"Blocked: this exact call has returned exactly the same thing {again[1]} "
                "times in this turn. Whatever was done in between did not change it, and "
                "asking again will not.",
                key, tool_use_id)
        # Checked before the polling exemption: `tail_remote_log` is asked
        # again on purpose while a job runs, but not after it has failed the
        # same way three times — that is an unreachable host, not a job to
        # watch.
        strikes = self._failure_strikes(key, time.monotonic())
        if strikes >= FAILED_REPEAT_LIMIT:
            label = name.split("__")[-1] if name.startswith("mcp__") else name
            if tool_use_id:
                self.counted.add(tool_use_id)
            if not self.looping:
                self.looping = label
                self.stop_reason = (f"{label} failed or was refused the same way {strikes} "
                                    f"times and was made again — {self.failures[key][1]}")
                self.stop_hint = ("It was not run again. Fix what the error names, or tell "
                                  "the model what to do instead.")
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
            self.outputs.clear()
            self.calls.clear()
            self.same_result.clear()
            return {}
        if not name.startswith("mcp__"):
            # Host tools are never denied. The rule is about MCP calls; a
            # third identical `Read` (read → edit → read → edit → read) or a
            # `Monitor` with the same until-loop — the very thing the Bash
            # nudge below recommends — used to be denied, and at five the turn
            # was interrupted.
            if name == "Bash":
                if first:
                    # A command not yet run this turn may have changed what the
                    # others print — test, fix with sed, test again is not a
                    # loop. One already run, with the same output, has not.
                    self.outputs.clear()
                same = self.outputs.get(key)
                if same and same[1] >= BASH_SAME_OUTPUT:
                    return self._refuse(
                        "Bash",
                        f"Blocked: this exact command has already run {same[1]} times in this "
                        "turn and printed exactly the same output every time. Running it again "
                        "cannot print anything new. If you are waiting for the output to change, "
                        "that is `Monitor` with an until-loop (or `wait_for_run` for a recorded "
                        "run), not the same command again.",
                        key, tool_use_id)
                seen = self.repeats[key] = self.repeats.get(key, 0) + 1
                # Only for a command that has RUN: the note is about polling,
                # and it was given to a command refused five times in a row.
                if seen == BASH_NUDGE and key in self.returned:
                    return _note(
                        "This Bash command has run identically several times. If it is polling for a "
                        "log line or a file to appear, use `Monitor` with an until-loop instead — that "
                        "backs off, times out cleanly, and does not spend a turn per check."
                    )
            return {}
        if is_scivo_write(name) and first:
            # A write may change what every read returns: forget the reads
            # counted so far (this call's own count stays, so the same write
            # repeated verbatim is still a loop). Only the FIRST time it is
            # made this turn: "write" is read off the tool's name, and on a
            # server that is not ours a query tool is called `query`. Three of
            # those asked in rotation each wiped the other two's counts, so
            # forty-five calls went by without one refusal.
            self.repeats = {key: self.repeats.get(key, 0)}
            self.outputs.clear()
        seen = self.repeats[key] = self.repeats.get(key, 0) + 1
        if seen < SAME_CALL_LIMIT:
            return {}
        label = name.split("__")[-1]
        if seen >= SAME_CALL_STOP and not self.looping:
            self.looping = label
            self.stop_reason = (f"{label} was called the same way {SAME_CALL_STOP} times "
                                "in this turn")
        return self._refuse(
            label,
            f"Blocked: `{label}` has already run {seen - 1} times in this turn with exactly "
            "these arguments, and returned each time. Calling it again cannot produce anything "
            "new.",
            key, tool_use_id)

    async def on_compact(self, payload: Any, tool_use_id: str | None,
                         context: Any) -> dict[str, Any]:
        """Ask for the handoff while the detail is still here.

        Whether `additionalContext` is honoured on this event is not something
        this can check from in here, so the hook also PRINTS that it fired:
        "the hook never ran" and "the hook ran and the model ignored it" are
        different problems, and a silent automation that turns out to do
        nothing is the failure mode worth spending a line of output on.
        `/handoff` does the same thing on demand either way.
        """
        if not handoff_enabled():
            return {}
        print(ui.dim("\n  compacting — asking for a Scivo handoff first "
                     "(/notify-style off switch: SCIVO_HANDOFF=off)"), flush=True)
        return {"hookSpecificOutput": {"hookEventName": "PreCompact",
                                       "additionalContext": HANDOFF_ASK}}

    async def on_bash(self, payload: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        written = str(payload.get("tool_input", {}).get("command", ""))
        # What the shell will RUN, without the file contents it writes.
        command = executable_text(written)

        if RAW_REMOTE_JOB.search(command) and marked_setup(written):
            # Not a way round the rule: the mark turns the refusal into a
            # question for the PERSON. In the hook file the same mark lets the
            # command straight through, and here there was no mark at all —
            # so starting a server on a registered machine had no path, and a
            # model that went looking for one had nobody to ask (user,
            # 2026-11-13: "승인요청 하도록 하자"). In a session that asks about
            # nothing (bypassPermissions) this is not asked either; that mode
            # says so about remote jobs when it is switched on.
            self.blocked.append("held: raw remote launch marked setup")
            return _ask(
                "This starts something on a remote machine through raw ssh, in the "
                "background, with NO run record. The model marked it as setup — not a "
                "job whose result is used.\n"
                "Yes runs it exactly as written: no row in the Runs tab, and nothing here "
                "will track it or stop it.\n"
                "For an analysis, a render or anything a result comes out of, say no: "
                "`submit_remote_job` launches the same command with the record."
            )

        if RAW_REMOTE_JOB.search(command):
            self.blocked.append("raw remote job")
            if self.loaded is not None and "submit_remote_job" not in self.loaded:
                # Never send the model to a tool it does not have.
                return _deny(
                    "Blocked: a remote job launched through raw ssh leaves no run record, so "
                    "the Runs tab cannot say which machine, command and parameters produced "
                    "the result.\n"
                    "The tool that launches it WITH a record, `submit_remote_job`, is not "
                    "loaded in this session (a read-only session, or a profile that carries "
                    "no scivo tools). There is no way to start this job from here, and "
                    "rewording the command will be refused the same way.\n"
                    "Stop and tell the user: what you were going to launch and on which "
                    "server, and that they can run it themselves or restart the session "
                    "with its tools (`/profile` lists the choices). If it is a setup step "
                    "and not a job, `# setup` on the first line asks them here instead."
                )
            return _deny(
                "Blocked: a remote job launched through raw ssh leaves no run record, so the "
                "Runs tab cannot say which machine, command and parameters produced the result.\n"
                "Use `mcp__scivo__submit_remote_job(...)` instead — it creates the record "
                "(host, command, env_name, log_path, pid) as it launches.\n"
                "No paper in this project (a video or tooling project)? Pass "
                "`slug=\"_project\"` — the run record is the point, and it does not need "
                "a manuscript to hang from. A one-off render or image generation counts.\n"
                "If this really is not a result-producing command (a copy, a cleanup), "
                "say so to the user and run it in the foreground.\n"
                "If it is not a job at all — a service to start, a one-time setup step — "
                "put `# setup` alone on the first line of the command: the user is then "
                "asked, and it runs only if they say yes. That is a question to a person, "
                "not a switch; do not use it for work whose result is used."
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

        if (hit := kills_this_session(command)) is not None:
            self.blocked.append("pattern kill matching this session")
            return _deny(
                "Blocked: that pattern matches this session's own process —\n"
                f"  {hit}\n"
                "These commands send SIGTERM by default, so this would stop the "
                "Claude Code process running this conversation (exit 143) rather "
                "than the job you meant.\n"
                "Name the PID instead: `ps -eo pid,etime,args | grep '[p]attern'` "
                "to find it, then `kill <pid>`. For a job this session launched, "
                "`mcp__scivo__kill_remote_job` closes the run record too."
            )

        if analysis_shaped(command):
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
        hits = [label for probe, label in COMPUTE_SPECS
                if (probe(text) if callable(probe) else probe.search(text))]
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


def handoff_enabled() -> bool:
    """`SCIVO_HANDOFF=off` turns the automatic handoff off. On by default: a
    compaction is the one moment where detail is discarded for good, and the
    person who would have asked for a handoff is usually not watching."""
    return (os.environ.get("SCIVO_HANDOFF", "").strip().lower() or "on") not in (
        "off", "0", "no", "false")


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
            HookMatcher(hooks=[rails.on_destination]),  # no matcher: every tool
            HookMatcher(hooks=[rails.on_any_tool]),   # no matcher: every tool
        ],
        # A failing command is only visible after it runs, and the loop this
        # catches repeats once per turn — so the count has to live outside the
        # per-turn counters above, fed by what actually happened.
        "PostToolUseFailure": [HookMatcher(hooks=[rails.on_tool_failure])],
        "PostToolUse": [HookMatcher(hooks=[rails.on_tool_success])],
        # The one moment detail is discarded for good. `PreCompact` is the only
        # compaction event there is — there is no PostCompact — so the handoff
        # is asked for BEFORE the summary exists, which is also better: we want
        # what the session knows, not a rewrite of Claude Code's summary.
        "PreCompact": [HookMatcher(hooks=[rails.on_compact])],
    }
