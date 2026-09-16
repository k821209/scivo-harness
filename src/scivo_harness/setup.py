"""`scivo setup` — wiring a directory to a Scivo project.

Three steps and a check. The check is the point: a project id that does not
match is reported here, at setup, instead of surfacing as a confusing warning in
some later session.

What this deliberately does NOT do is search for a Python that can import the
MCP. The dashboard's shell script spends twenty lines on that, because it writes
`python3` into `.mcp.json` and PATH resolves differently under another shell or
conda environment — "it worked yesterday", with nothing to read. Here the MCP is
a dependency of the harness, so the interpreter is the one running this code and
there is nothing to search for.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MCP_TEMPLATE_KEYS = ("type", "command", "args", "env")
IGNORE_BLOCK = """
# scivo: holds this project's API key in clear text
.mcp.json

# scivo: local model endpoints may carry a token
.scivo/
"""

CLAUDE_MD = """# Scivo project: {name}

Project id: `{project_id}`
Dashboard: https://co-scientist-5af1a.web.app/projects/{project_id}/papers

Under `scivo`, the session-start protocol runs in the harness before the first
token and its results are already in the system prompt — see `scivo status`.
Under other hosts, call `mcp__scivo__project_guide()` for the conventions.
"""


class SetupError(RuntimeError):
    pass


@dataclass
class Result:
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def did(self, message: str) -> None:
        self.steps.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def mcp_config(key: str, interpreter: str | None = None) -> dict:
    return {
        "mcpServers": {
            "scivo": {
                "type": "stdio",
                "command": interpreter or sys.executable,
                "args": ["-m", "co_scientist_local"],
                "env": {"CO_SCIENTIST_API_KEY": key},
            }
        }
    }


def check_mcp_importable(interpreter: str | None = None) -> None:
    interpreter = interpreter or sys.executable
    probe = subprocess.run(
        [interpreter, "-c", "import co_scientist_local"], capture_output=True, text=True
    )
    if probe.returncode != 0:
        raise SetupError(
            f"{interpreter} cannot import co_scientist_local.\n"
            "It normally installs as a dependency of this harness. Reinstall with:\n"
            '  pip install --upgrade "scivo-harness @ git+https://github.com/k821209/scivo-harness.git"'
        )


def write_mcp_json(root: Path, key: str, force: bool, result: Result) -> tuple[Path, Path | None]:
    path = root / ".mcp.json"
    backup: Path | None = None
    if path.exists():
        if not force:
            raise SetupError(
                f"{path} already exists. This directory is set up.\n"
                "Pass --force to replace it (the old file is kept as .mcp.json.bak)."
            )
        backup = path.with_suffix(".json.bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        result.warn(f"replaced {path.name}; previous config kept as {backup.name}")
    path.write_text(json.dumps(mcp_config(key), indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)  # it holds a credential
    result.did(f"wrote {path.name} (interpreter {sys.executable})")
    return path, backup


def roll_back(path: Path, backup: Path | None) -> str:
    """Undo a failed setup without costing the user a working config.

    Removing the file we just wrote is right; leaving them with only a .bak
    after they passed --force is not — they came in with something that worked.
    """
    path.unlink(missing_ok=True)
    if backup is None or not backup.exists():
        return f"{path.name} was removed."
    path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
    path.chmod(0o600)
    backup.unlink(missing_ok=True)
    return f"{path.name} was restored from the backup; nothing changed."


def ensure_gitignore(root: Path, result: Result) -> None:
    path = root / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if ".mcp.json" in existing:
        return
    path.write_text(existing + IGNORE_BLOCK, encoding="utf-8")
    result.did("added .mcp.json and .scivo/ to .gitignore")


def ensure_claude_md(root: Path, project_id: str | None, name: str, result: Result) -> None:
    """Write one only when absent — an existing CLAUDE.md is the user's.

    It carries the project id, which is what later sessions compare against the
    key's binding. Without it that guard is simply skipped.
    """
    path = root / "CLAUDE.md"
    if path.exists():
        if project_id and project_id not in path.read_text(encoding="utf-8"):
            result.warn("CLAUDE.md exists but does not name this project id — "
                        "the mismatch guard will not run. Add it, or delete the file.")
        return
    if not project_id:
        return
    path.write_text(CLAUDE_MD.format(project_id=project_id, name=name), encoding="utf-8")
    result.did("wrote CLAUDE.md")


def link_skills(root: Path, result: Result) -> None:
    probe = subprocess.run(
        [sys.executable, "-m", "co_scientist_local", "install-skills", "--dir", str(root)],
        capture_output=True, text=True,
    )
    if probe.returncode == 0:
        result.did("linked skills into .claude/skills")
    else:
        result.warn("skill install failed; the MCP re-links them on startup. "
                    + (probe.stdout + probe.stderr).strip()[-200:])
