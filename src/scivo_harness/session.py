"""Assembling the agent session from the project's own state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import warnings

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import CanUseToolShadowedWarning

from . import guardrails, prompt, toolsets
from .config import ScivoConfig
from .providers import Endpoint, Provider, get as get_provider, prepare
from .preflight import Briefing, gather
from .scivo_mcp import connect
from .toolsets import PREFIX, ToolPlan

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"

# Host tools the research work actually uses. Everything else the preset offers
# stays available; these are the ones that never need a permission prompt.
AUTO_APPROVED_HOST_TOOLS = ["Read", "Glob", "Grep", "TodoWrite", "WebSearch", "WebFetch"]

# A local model gets the whole prompt on every request, uncached and with no
# deferred tool loading: Claude Code only defers tool schemas for Anthropic's
# own models. Measured on qwen-test, the full session was 377 KB (~100k tokens):
# 285 KB of tool schemas (272 tools) and 89 KB of system prompt, 71 KB of it the
# project guide. A 27B model spends minutes on that before its first token, so
# a local session carries a working core instead.
LOCAL_HOST_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "TodoWrite", "WebFetch",
                    "Skill", "AskUserQuestion", "Monitor"]
LOCAL_SCIVO_TOOLS = (
    "whoami", "list_papers", "get_paper_state", "get_manuscript", "list_sections",
    "get_section", "add_section", "update_section", "list_references",
    "search_references", "add_reference_by_doi", "list_figures", "get_figure",
    "list_tables", "get_table", "list_paper_comments", "count_open_user_comments",
    "list_analyses", "get_analysis", "list_todos", "add_todo", "update_todo",
    "get_project_memory", "append_project_memory", "update_project_memory",
    "log_activity", "report_feedback",
    # Every tool a guardrail message sends the model to. A block that says "use
    # submit_remote_job" in a session without it is a dead end.
    "list_servers", "get_server", "add_server", "update_server", "add_server_env",
    "submit_remote_job", "tail_remote_log", "poll_remote_pids",
    "create_analysis", "record_analysis_run",
)

# Everything else comes in by domain, and only when asked for: `--profile
# video` on a local model loads the 16 video tools on top of the core, because
# a session that cannot call `add_video` cannot do the video work at all. The
# paper domain is left out — it is 115 tools, and its everyday half is already
# in the core list above.
LOCAL_DOMAIN_LIMIT = 60


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
    endpoint: Any = None
    # The Claude-shaped options, kept so a switch back from a local model
    # restores the guide and the full tool surface.
    full_options: Any = None
    lean_append: str = ""
    scivo_tools: tuple[str, ...] = ()
    resumed: str | None = None
    local_tools: int | None = None

    def options_for(self, provider: Provider, **changes: Any) -> ClaudeAgentOptions:
        """Options for `provider`: the full session, or the lean one for a local model."""
        base = replace(self.full_options, **changes)
        if not provider.is_local:
            return base
        return lean(base, self.lean_append, self.scivo_tools, self.plan.profile)


def local_kit(scivo_tools: tuple[str, ...], profile: str) -> set[str]:
    """The scivo tools a local session carries: the core, plus asked-for domains."""
    keep = set(LOCAL_SCIVO_TOOLS) & set(scivo_tools)
    if profile == "full":
        return keep
    sizes: dict[str, list[str]] = {}
    for name in scivo_tools:
        sizes.setdefault(toolsets._domain_of(name), []).append(name)
    for domain in toolsets.PROFILES.get(profile, ()):  # the profile's own domains
        members = sizes.get(domain, [])
        if len(members) <= LOCAL_DOMAIN_LIMIT:
            keep.update(members)
    return keep


def lean(options: ClaudeAgentOptions, append: str, scivo_tools: tuple[str, ...],
         profile: str = "full") -> ClaudeAgentOptions:
    keep = local_kit(scivo_tools, profile)
    dropped = [PREFIX + n for n in scivo_tools if n not in keep]
    return replace(
        options,
        tools=list(LOCAL_HOST_TOOLS),
        system_prompt={"type": "preset", "preset": "claude_code", "append": append},
        disallowed_tools=sorted(set(options.disallowed_tools) | set(dropped)),
    )


async def survey(config: ScivoConfig, *, with_guide: bool) -> tuple[Briefing, list[str], str | None]:
    """One MCP connection: the briefing, the tool list, and optionally the guide.

    The agent gets its own connection to the same server; this one is the
    harness's, and it closes before the conversation starts.
    """
    async with connect(config) as client:
        briefing = await gather(client, config)
        from .update import drop_inapplicable_warning

        briefing.identity = drop_inapplicable_warning(config, briefing.identity)
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
    subscription: bool = False,
) -> Session:
    chosen = provider if isinstance(provider, Provider) else get_provider(provider)
    model = model or chosen.model or DEFAULT_MODEL

    briefing, tool_names, guide = await survey(config, with_guide=with_guide)
    import asyncio

    endpoint: Endpoint = await asyncio.to_thread(
        lambda: prepare(chosen, subscription=subscription))
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
        # Claude Code keeps an MCP discovery cache (tool list + schemas) that
        # can outlive an MCP update: after `git pull` the server had new
        # parameters and the session still saw the old schema. The CLI reads
        # this switch from its environment; off means every start discovers.
        env={"MCP_DISCOVERY_CACHE": "false", **endpoint.env},
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
        # One stdio message can carry a whole image: a rendered slide read back
        # for checking is a few MB of base64, and the SDK's 1 MB default killed
        # the turn with "JSON message exceeded maximum buffer size". Deck and
        # figure work does that routinely.
        max_buffer_size=64 * 1024 * 1024,
    )
    session = Session(options=options, briefing=briefing, plan=plan, rails=rails,
                      config=config, provider=chosen, model=model, approver=approver,
                      endpoint=endpoint, full_options=options,
                      lean_append=prompt.build(briefing, plan, guide=None),
                      scivo_tools=tuple(tool_names), resumed=resume)
    session.options = session.options_for(chosen)
    if chosen.is_local:
        session.local_tools = len(local_kit(tuple(tool_names), profile))
    return session


def scivo_tool_label(name: str) -> str:
    return name[len(PREFIX):] if name.startswith(PREFIX) else name
