"""Turning the CLI's failures into something a researcher can act on.

The Agent SDK reports a failed run by raising with the CLI's message attached.
Unhandled, that reaches the user as a Python traceback ending in a 401 — the
first thing a person sees when they have not signed in, and the least useful.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

AUTH_MARKERS = ("authenticate", "401", "api key is invalid", "not logged in",
                "invalid api key", "oauth", "unauthorized")
OVERLOAD_MARKERS = ("429", "rate limit", "overloaded", "529")
REFUSAL_MARKERS = ("refusal", "declined to", "stop_details")
# The API refuses a model the running CLI is too old to drive, and says so by
# naming a minimum version: "Claude Code 2.1.274 does not support this model;
# version 2.1.280 or newer is required. Run 'claude update' …". That advice is
# wrong for a session here, which is what makes it expensive — see explain().
CLI_TOO_OLD_MARKERS = ("does not support this model", "or newer is required")


def bundled_cli() -> Path | None:
    """The Claude Code carried inside claude_agent_sdk, if this install has one."""
    try:
        import claude_agent_sdk
    except Exception:  # noqa: BLE001
        return None
    name = "claude.exe" if sys.platform == "win32" else "claude"
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return path if path.is_file() else None


def cli_in_use() -> Path | None:
    """The Claude Code a session will actually spawn.

    The SDK tries its BUNDLED copy before anything on PATH
    (`_find_cli` → `_find_bundled_cli`, subprocess_cli.py). So on a machine
    with Claude Code installed, `claude --version` and the version a scivo
    session runs are two different numbers, and the one on PATH is the one
    that does not matter. Getting this round the wrong way is how a user runs
    `claude update`, is told it succeeded, and hits the identical version
    error on the next turn with nothing left to try.
    """
    return bundled_cli() or (Path(found) if (found := shutil.which("claude")) else None)


def cli_version(path: Path | None) -> str | None:
    """`x.y.z` from `<path> --version`, or None if it cannot be read."""
    if path is None:
        return None
    try:
        done = subprocess.run([str(path), "--version"], capture_output=True,
                              text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", done.stdout or "")
    return match.group(1) if match else None


def _parts(version: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in (version or "").split("."))
    except ValueError:
        return ()


def cli_outpaced() -> tuple[str, str | None]:
    """Whether `claude update` has outpaced the copy this session runs.

    Three outcomes, and the third is why this returns a pair:

      ("ok", None)            compared, and the session's copy is not behind
      ("outpaced", sentence)  PATH is newer — what a `claude update` leaves
                              behind when the SDK carries its own binary
      ("unknown", why)        could NOT compare, naming what was missing

    Derived, not guessed: it needs no knowledge of what any model requires.
    The first version of this collapsed "unknown" into "ok" by returning None
    for both, which is the shape this project forbids — a caller could not
    tell a passing check from a check that never ran, and the permissive
    reading is the one that gets believed.
    """
    bundled = bundled_cli()
    if bundled is None:
        # PATH's copy IS the one in use, so there is nothing to be behind.
        return ("ok", None)
    on_path = shutil.which("claude")
    if not on_path:
        return ("ok", None)
    mine = cli_version(bundled)
    theirs = cli_version(Path(on_path))
    if not _parts(mine):
        return ("unknown", f"cannot read the version of {bundled}")
    if not _parts(theirs):
        return ("unknown", f"cannot read the version of {on_path}")
    if _parts(theirs) <= _parts(mine):
        return ("ok", None)
    return ("outpaced",
            f"Claude Code on PATH is {theirs}, but this session runs the copy "
            f"bundled in claude-agent-sdk, which is {mine}. `claude update` "
            f"cannot change that one. To move it:\n"
            f"  {sys.executable} -m pip install -U claude-agent-sdk\n"
            f"then restart the session — a running process keeps the binary it "
            f"started with.")


def claude_binary() -> str:
    """The binary to point the user at: theirs if installed, else the bundled one."""
    found = shutil.which("claude")
    if found:
        return found
    import claude_agent_sdk

    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    return str(bundled) if bundled.exists() else "claude"


def explain(error: Exception) -> str | None:
    """A better message for the failures worth explaining, else None."""
    text = str(error).lower()

    if any(marker in text for marker in AUTH_MARKERS):
        # Name the binary that exists on THIS machine rather than a command
        # that may not resolve: `claude` when it is on PATH, the SDK's bundled
        # copy otherwise. The old text said `scivo login` for everyone, which
        # was safe but told a user with Claude Code installed to learn a second
        # name for the same thing (user, 2026-10-09).
        # The two spellings are not interchangeable: `claude auth login` vs
        # `scivo login`. Pick by what resolves here.
        login = "claude auth login" if shutil.which("claude") else "scivo login"
        return (
            "Not signed in.\n\n"
            f"  {login}              (a Claude subscription)\n"
            f"  {login} --console    (Anthropic Console, API billing)\n\n"
            "scivo does not handle sign-in: it supplies no credentials of its own and "
            "stores none. Signing in happens in Claude Code itself, which is where "
            "Anthropic requires it to happen.\n"
            "Already signed in? An ANTHROPIC_API_KEY in your environment takes "
            "precedence over that login — check with `scivo doctor`.\n"
            "No `claude` on PATH? `scivo login` runs the copy bundled in the Agent "
            "SDK; `scivo doctor` prints the path it resolves.\n"
            "Using a local model instead? `scivo --provider <name>`."
        )

    if "maximum buffer size" in text:
        return (
            "One message from Claude Code was too large to read in.\n\n"
            "scivo allows 64 MB per message, which covers rendered slides and figures. "
            "Something bigger than that came back — usually a very large file read whole.\n"
            "Read it in parts (`offset`/`limit`, or `head`), or point the model at a "
            "smaller rendering of it. The session itself is fine; only this turn was lost."
        )

    if "prompt is too long" in text or "exceeds the available context" in text:
        return (
            "The conversation no longer fits in this model's context window.\n\n"
            "scivo reads the window from a local server (`/props`) and tells the CLI, "
            "which compacts the conversation before it gets here. Seeing this anyway "
            "means the window is smaller than reported or was never reported.\n"
            "Set it explicitly in providers.toml — `context_tokens = <the server's -c "
            "value>` — or start a fresh session and resume only what you need.\n"
            "Restarting the server with a larger `-c` works too, if it has the memory."
        )

    if any(marker in text for marker in REFUSAL_MARKERS):
        return (
            "The model declined this request.\n\n"
            "Nothing in this harness filters content — the refusal came from the "
            "model, and it is reported rather than hidden.\n"
            "If the work is legitimate and the phrasing tripped it, the usual fix "
            "is to say what the result is for: the paper, the method, the reader. "
            "Scientific subjects that sound sensitive are ordinary work, and the "
            "context is what makes that visible.\n"
            "If it keeps declining something you believe it should do, that is "
            "worth reporting — `report_feedback` for the harness, and Anthropic "
            "for the model."
        )

    if any(marker in text for marker in OVERLOAD_MARKERS):
        return ("The model endpoint is rate limited or overloaded. Wait and retry; "
                "`--effort low` and a narrower `--profile` both cost less per turn.")

    if all(marker in text for marker in CLI_TOO_OLD_MARKERS):
        # The API's own remedy — "Run 'claude update', or update the Claude
        # desktop app" — is written for someone running Claude Code directly.
        # Neither reaches a session here: the SDK spawns its OWN bundled copy
        # before anything on PATH, and `scivo update` will not move it either
        # (the pin is a floor, and the dep top-up only installs packages that
        # are MISSING). So a user can follow both instructions, see both
        # report success, and get the same error every turn. Name the binary
        # that is actually running, and the one command that changes it.
        in_use = cli_in_use()
        version = cli_version(in_use)
        return (
            "The model is newer than the Claude Code this session runs.\n\n"
            f"  in use   {in_use or 'not found'}"
            + (f"   ({version})" if version else "") + "\n"
            + (f"  on PATH  {shutil.which('claude')}"
               f"   ({cli_version(Path(shutil.which('claude')))})\n"
               if shutil.which("claude") and bundled_cli() else "")
            + "\nThe SDK spawns its bundled copy before anything on PATH, so "
            "`claude update` does not reach it and neither does `scivo "
            "update`. Upgrade the package that carries it:\n\n"
            f"  {sys.executable} -m pip install -U claude-agent-sdk\n\n"
            "Then restart the session — a running process keeps the binary it "
            "started with. Or pick a model the current CLI supports with "
            "`/model`."
        )

    if "not found" in text and "claude" in text:
        return ("The Claude Code binary could not be found. It normally ships inside "
                "claude-agent-sdk; reinstall it, or install Claude Code and put it on PATH.")
    return None
