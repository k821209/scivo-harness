"""A direct MCP client for the scivo server.

The agent reaches these tools through the model. The harness also needs to call
some of them *before* the first token — the session-start protocol is a fixed
list of facts, not a judgement, and a model that is merely asked to fetch them
is a model that can skip them. So preflight speaks MCP itself and puts the
answers in the prompt as facts.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import ScivoConfig


@dataclass
class ToolOutcome:
    """A tool call that either produced items or a reason it did not.

    `items` is always a list because that is what the protocol gives us: the
    server emits one content block per record, so a list of nine servers
    arrives as nine blocks and an empty list arrives as no blocks at all.
    Scalar tools (`whoami`, `get_project_memory`) come back as a single block —
    read those through `first`.
    """

    ok: bool
    items: list[Any] = field(default_factory=list)
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok

    @property
    def first(self) -> Any:
        return self.items[0] if self.items else None

    def __len__(self) -> int:
        return len(self.items)


def _unwrap(payload: Any) -> Any:
    """co_scientist_local returns JSON text, often wrapped in {"result": ...}."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return payload
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


class ScivoClient:
    """Thin wrapper: every call returns a ToolOutcome instead of raising.

    Preflight calls a dozen tools and must survive any one of them being absent
    or renamed — a harness that refuses to start because `list_datasets` moved
    is worse than one that starts and says so.
    """

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def call(self, name: str, **arguments: Any) -> ToolOutcome:
        try:
            result = await self._session.call_tool(name, arguments or None)
        except Exception as exc:  # noqa: BLE001 - any transport/protocol failure
            return ToolOutcome(False, error=f"{type(exc).__name__}: {exc}")

        blocks = _text_blocks(result)
        if getattr(result, "isError", False):
            return ToolOutcome(False, error="\n".join(blocks) or "tool reported an error")
        # The server reports refusals as an ordinary result whose text starts
        # with "Error:", so a successful transport is not a successful call.
        if len(blocks) == 1 and blocks[0].startswith(("Error:", "Unknown tool:")):
            return ToolOutcome(False, error=blocks[0])
        return ToolOutcome(True, items=[_unwrap(b) for b in blocks])


def _text_blocks(result: Any) -> list[str]:
    blocks = getattr(result, "content", None) or []
    return [b.text for b in blocks if getattr(b, "type", None) == "text"]


@asynccontextmanager
async def connect(config: ScivoConfig):
    """Open a stdio session to the project's scivo MCP server."""
    params = StdioServerParameters(
        command=config.command,
        args=config.args,
        env=config.child_env(),
        cwd=str(config.root),
    )
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        async with stdio_client(params, errlog=devnull) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield ScivoClient(session)
