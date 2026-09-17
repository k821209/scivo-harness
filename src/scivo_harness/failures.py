"""Turning the CLI's failures into something a researcher can act on.

The Agent SDK reports a failed run by raising with the CLI's message attached.
Unhandled, that reaches the user as a Python traceback ending in a 401 — the
first thing a person sees when they have not signed in, and the least useful.
"""

from __future__ import annotations

import shutil
from pathlib import Path

AUTH_MARKERS = ("authenticate", "401", "api key is invalid", "not logged in",
                "invalid api key", "oauth", "unauthorized")
OVERLOAD_MARKERS = ("429", "rate limit", "overloaded", "529")
REFUSAL_MARKERS = ("refusal", "declined to", "stop_details")


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
        return (
            "Not signed in.\n\n"
            "  scivo login              (a Claude subscription)\n"
            "  scivo login --console    (Anthropic Console, API billing)\n\n"
            "scivo does not handle sign-in: it supplies no credentials of its own and "
            "stores none. Signing in happens in Claude Code itself, which is where "
            "Anthropic requires it to happen.\n"
            "Already signed in? An ANTHROPIC_API_KEY in your environment takes "
            "precedence over that login — check with `scivo doctor`.\n"
            "Using a local model instead? `scivo --provider <name>`."
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

    if "not found" in text and "claude" in text:
        return ("The Claude Code binary could not be found. It normally ships inside "
                "claude-agent-sdk; reinstall it, or install Claude Code and put it on PATH.")
    return None
