"""Assembling the agent session from the project's own state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

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
    resume: str | None = None,
    max_budget_usd: float | None = None,
) -> Session:
    chosen = provider if isinstance(provider, Provider) else get_provider(provider)
    model = model or chosen.model or DEFAULT_MODEL

    briefing, tool_names, guide = await survey(config, with_guide=with_guide)
    plan = toolsets.plan(tool_names, profile=profile, read_only=read_only)
    rails = guardrails.Guardrails()

    mcp_server: dict[str, Any] = {
        "type": "stdio",
        "command": config.command,
        "args": config.args,
        "env": config.env,
    }

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
        permission_mode="default",
        model=model,
        # A local model behind a shim has no effort parameter; sending one is
        # a 400 from some proxies and silently ignored by others.
        effort=effort if chosen.supports_effort else None,
        env=chosen.resolve_env(),
        cwd=str(config.root),
        resume=resume,
        max_budget_usd=max_budget_usd,
        include_partial_messages=True,
    )
    return Session(options=options, briefing=briefing, plan=plan, rails=rails,
                   config=config, provider=chosen, model=model)


def scivo_tool_label(name: str) -> str:
    return name[len(PREFIX):] if name.startswith(PREFIX) else name
