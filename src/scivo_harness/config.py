"""Where the harness finds the project binding, the API key and the MCP command.

We read the same `.mcp.json` the dashboard's Setup tab writes, so a directory
that already works under another host keeps working here with no second copy of
the key. `CLAUDE.md` is read only for the project id, which is the one fact the
two files can disagree about.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

MCP_FILE = ".mcp.json"
SERVER_KEY = "scivo"
PROJECT_ID_RE = re.compile(r"Project id:\s*`([A-Za-z0-9_-]+)`")


class ConfigError(RuntimeError):
    """The project directory is not wired to a Scivo project."""


@dataclass(frozen=True)
class ScivoConfig:
    root: Path
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    expected_project_id: str | None = None
    source: str = MCP_FILE

    @property
    def api_key(self) -> str | None:
        return self.env.get("CO_SCIENTIST_API_KEY")

    def child_env(self) -> dict[str, str]:
        """Full environment for the MCP subprocess: ours plus the key."""
        return {**os.environ, **self.env}


def find_root(start: Path | None = None) -> Path:
    """Nearest ancestor holding a .mcp.json, else the starting directory."""
    start = (start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / MCP_FILE).is_file():
            return candidate
    return start


def _read_project_id(root: Path) -> str | None:
    claude_md = root / "CLAUDE.md"
    if not claude_md.is_file():
        return None
    match = PROJECT_ID_RE.search(claude_md.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def load(root: Path | None = None) -> ScivoConfig:
    root = find_root(root)
    mcp_path = root / MCP_FILE
    expected = _read_project_id(root)

    if mcp_path.is_file():
        try:
            servers = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        except (json.JSONDecodeError, KeyError) as exc:
            raise ConfigError(f"{mcp_path} is not a readable MCP config: {exc}") from exc
        if SERVER_KEY not in servers:
            # The rename landed 2026-09-09; an older file still says co_scientist.
            legacy = next((k for k in servers if "scientist" in k or "scivo" in k), None)
            if legacy is None:
                raise ConfigError(
                    f"{mcp_path} has no '{SERVER_KEY}' server. Re-run the Setup tab script."
                )
            raise ConfigError(
                f"{mcp_path} names the server '{legacy}'. Rename it to '{SERVER_KEY}' — "
                "every skill's tool reference misses otherwise."
            )
        spec = servers[SERVER_KEY]
        return ScivoConfig(
            root=root,
            command=spec.get("command", sys.executable),
            args=list(spec.get("args", ["-m", "co_scientist_local"])),
            env=dict(spec.get("env", {})),
            expected_project_id=expected,
            source=str(mcp_path),
        )

    key = os.environ.get("CO_SCIENTIST_API_KEY") or os.environ.get("SCIVO_API_KEY")
    if not key:
        raise ConfigError(
            f"This directory is not set up for a Scivo project (no {MCP_FILE} under {root}).\n"
            "In your project's directory, run:\n"
            "  scivo setup --key <project key> --project <project id>\n"
            "Both are on the project's Setup tab in the Scivo dashboard."
        )
    return ScivoConfig(
        root=root,
        command=os.environ.get("SCIVO_PYTHON", sys.executable),
        args=["-m", "co_scientist_local"],
        env={"CO_SCIENTIST_API_KEY": key},
        expected_project_id=expected,
        source="environment",
    )
