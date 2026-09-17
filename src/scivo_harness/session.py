"""Assembling the agent session from the project's own state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import warnings

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import CanUseToolShadowedWarning

from . import guardrails, prompt, toolsets
from .config import ScivoConfig
from .providers import Provider, get as get_provider
from .preflight import Briefing, gather
from .scivo_mcp import connect
from .toolsets import PREFIX, ToolPlan

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"

# Host tools the research work actually uses. Everything else the preset offers
# stays available; these are the ones that never need a permission prompt.
AUTO_APPROVED_HOST_TOOLS = ["Read", "Glob", "Grep", "TodoWrite", "WebSearch", "WebFetch"]


@dataclass
class Session:
    options: ClaudeAgentOptions
    briefing: Briefing
    plan: ToolPlan
    rails: guardrails.Guardrails
    config: ScivoConfig
    provider: Provider
    model: str
    approver: Any = None


async def survey(config: ScivoConfig, *, with_guide: bool) -> tuple[Briefing, list[str], str | None]:
    """One MCP connection: the briefing, the tool list, and optionally the guide.

    The agent gets its own connection to the same server; this one is the
    harness's, and it closes before the conversation starts.
    """
    async with connect(config) as client:
        briefing = await gather(client, config)
        tools = await client._session.list_tools()  # noqa: SLF001 - our own wrapper
        names = [tool.name for tool in tools.tools]
        guide = None
        if with_guide:
            outcome = await client.call("project_guide")
            if outcome and isinstance(outcome.first, str):
                guide = outcome.first
            elif not outcome:
                briefing.errors.append(f"project_guide: {outcome.error}")
        return briefing, names, guide


async def build(
    config: ScivoConfig,
    *,
    profile: str = "full",
    read_only: bool = False,
    provider: str | Provider = "anthropic",
    model: str | None = None,
    effort: str = DEFAULT_EFFORT,
    with_guide: bool = True,
    interactive: bool = True,
    permission_mode: str = "default",
    resume: str | None = None,
    continue_last: bool = False,
    max_budget_usd: float | None = None,
    add_dirs: list[str] | None = None,
) -> Session:
    chosen = provider if isinstance(provider, Provider) else get_provider(provider)
    model = model or chosen.model or DEFAULT_MODEL

    briefing, tool_names, guide = await survey(config, with_guide=with_guide)
    plan = toolsets.plan(tool_names, profile=profile, read_only=read_only)
    rails = guardrails.Guardrails()

    # Without a callback there is no channel to ask on, so anything outside
    # `allowed_tools` is denied with a message nobody can act on. An explicit
    # mode means the user has already decided, so we stay out of the way.
    from .permissions import Approvals, Explain

    # Always installed, whatever mode the session starts in: the mode can now
    # change mid-session, and a session switched back to "default" with no
    # callback would deny everything with a message nobody can act on.
    approver = Approvals() if interactive else Explain(permission_mode, str(config.root))

    mcp_server: dict[str, Any] = {
        "type": "stdio",
        "command": config.command,
        "args": config.args,
        "env": config.env,
    }

    # The SDK warns that `allowed_tools` auto-approves before the callback runs.
    # That is the design: the read-only surface needs no approval, and asking
    # about `list_papers` would train people to answer yes without reading.
    warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

    options = ClaudeAgentOptions(
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": prompt.build(briefing, plan, guide=guide),
        },
        mcp_servers={"scivo": mcp_server},
        allowed_tools=plan.auto_approved + AUTO_APPROVED_HOST_TOOLS,
        disallowed_tools=plan.disallowed,
        hooks=guardrails.build(rails),
        setting_sources=["user", "project", "local"],
        skills="all",
        permission_mode=permission_mode,
        can_use_tool=approver,
        model=model,
        # A local model behind a shim has no effort parameter; sending one is
        # a 400 from some proxies and silently ignored by others.
        effort=effort if chosen.supports_effort else None,
        env=chosen.resolve_env(),
        cwd=str(config.root),
        add_dirs=list(add_dirs or []),
        # Makes bypassPermissions *available* without turning it on, so
        # `/dangerously-skip-permissions` can switch to it mid-session. Without
        # it the CLI refuses: "the session was not launched with
        # --dangerously-skip-permissions". Nothing is bypassed until chosen.
        extra_args={"allow-dangerously-skip-permissions": None},
        resume=resume,
        continue_conversation=continue_last and not resume,
        max_budget_usd=max_budget_usd,
        include_partial_messages=True,
    )
    return Session(options=options, briefing=briefing, plan=plan, rails=rails,
                   config=config, provider=chosen, model=model, approver=approver)


def scivo_tool_label(name: str) -> str:
    return name[len(PREFIX):] if name.startswith(PREFIX) else name
