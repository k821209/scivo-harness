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
import os
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


def mcp_config(key: str, interpreter: str | None = None, checkout: Path | None = None) -> dict:
    env = {"CO_SCIENTIST_API_KEY": key}
    if checkout is not None:
        # Run the MCP straight out of the clone, ahead of any installed copy.
        # This is what someone with a checkout means by having one: `git pull`
        # updates every project, with no pip step and nothing to keep in sync.
        env["PYTHONPATH"] = str(checkout / "apps" / "local-mcp")
    return {
        "mcpServers": {
            "scivo": {
                "type": "stdio",
                "command": interpreter or sys.executable,
                "args": ["-m", "co_scientist_local"],
                "env": env,
            }
        }
    }


def usable_checkout(interpreter: str | None = None) -> tuple[Path | None, str | None]:
    """A source checkout this interpreter can actually run, and why not if not.

    A machine with `~/co-scientist-mcp-public` on it has one for a reason, and
    the reason is that edits should take effect. Before this, setup wrote the
    installed snapshot instead and the session opened with a warning saying so,
    leaving the person to fix by hand what we could see from here.
    """
    from .update import checkout_for_mcp

    checkout = checkout_for_mcp()
    if checkout is None:
        return None, None
    package = checkout / "apps" / "local-mcp"
    # The server module, not just the package: importing the package alone
    # succeeded in an environment that could not actually start the server,
    # and setup then wrote a config whose MCP died on first contact.
    probe = subprocess.run(
        [interpreter or sys.executable, "-c",
         "import co_scientist_local.mcp_server as m; print(m.__file__)"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(package)},
    )
    if probe.returncode != 0:
        first = (probe.stderr or "").strip().splitlines()[-1:] or [""]
        return None, f"{checkout} cannot run here ({first[0]}); using the installed copy"
    if str(package) not in probe.stdout:
        return None, f"{checkout} did not take precedence; using the installed copy"
    return checkout, None


def environment_note() -> str | None:
    """Warn when scivo landed in a shared environment rather than its own.

    Hit by the first person to install this: `pip` resolved to a conda env that
    was not the active one — `CONDA_DEFAULT_ENV` said base while PATH put an
    env's bin first — so the install went somewhere they had not chosen. Two
    things followed. A second `scivo` appeared ahead of theirs on PATH, and
    `co-scientist-local`, pulled in as a dependency from git, replaced the
    EDITABLE install that every project on the machine was running. Both were
    silent; pip reported success.

    The install command cannot be made safe by wording, so say it here, where
    we know which interpreter actually ran.
    """
    import importlib.metadata as md
    import json

    prefix = Path(sys.prefix)
    dedicated = sys.prefix != sys.base_prefix and not (prefix / "conda-meta").is_dir()
    if dedicated:
        return None

    lines = [
        f"scivo is installed in a shared environment: {sys.prefix}",
        "  A dedicated virtualenv avoids two problems this one has:",
        "    · its `scivo` can sit ahead of another on PATH (see `scivo doctor`)",
        "    · installing here can replace an editable co-scientist-local that",
        "      other projects on this machine are running",
    ]
    try:
        raw = md.distribution("co-scientist-local").read_text("direct_url.json") or "{}"
        url = json.loads(raw)
        if not url.get("dir_info", {}).get("editable") and str(url.get("url", "")).startswith("http"):
            lines.append("  This environment's co-scientist-local is a snapshot from git, not an")
            lines.append("  editable checkout. If it used to be editable, restore it with:")
            lines.append("    pip install -e <your co-scientist-mcp-public>/apps/local-mcp --no-deps")
    except Exception:  # noqa: BLE001 - absent or unreadable metadata is not our problem here
        pass
    lines.append("  To move scivo out: python3 -m venv ~/.scivo && "
                 '~/.scivo/bin/pip install "scivo-harness @ '
                 'git+https://github.com/k821209/scivo-harness.git"')
    return "\n".join(lines)


def check_mcp_importable(interpreter: str | None = None, checkout: Path | None = None) -> None:
    interpreter = interpreter or sys.executable
    env = dict(os.environ)
    if checkout is not None:
        env["PYTHONPATH"] = str(checkout / "apps" / "local-mcp")
    probe = subprocess.run(
        [interpreter, "-c", "import co_scientist_local"], capture_output=True, text=True, env=env
    )
    if probe.returncode != 0:
        raise SetupError(
            f"{interpreter} cannot import co_scientist_local.\n"
            "It normally installs as a dependency of this harness. Reinstall with:\n"
            "  python3 -m venv ~/.scivo && ~/.scivo/bin/pip install \\\n"
            '    "scivo-harness @ git+https://github.com/k821209/scivo-harness.git"'
        )


def write_mcp_json(root: Path, key: str, force: bool, result: Result,
                   interpreter: str | None = None,
                   checkout: Path | None = None) -> tuple[Path, Path | None]:
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
    path.write_text(json.dumps(mcp_config(key, interpreter, checkout), indent=2) + "\n",
                    encoding="utf-8")
    path.chmod(0o600)  # it holds a credential
    result.did(f"wrote {path.name} (interpreter {interpreter or sys.executable})")
    if checkout is not None:
        result.did(f"the MCP runs from your checkout at {checkout} — `git pull` there updates it")
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


def point_at_checkout(root: Path, interpreter: str | None = None) -> str | None:
    """Make an existing project run the MCP from the checkout on this machine.

    A project set up before this — or on a machine where the clone arrived
    later — keeps running the installed snapshot, and every session opens with
    the warning saying edits do not take effect. The fix is one line in
    `.mcp.json`, so do it rather than print instructions for it.
    """
    path = root / ".mcp.json"
    if not path.is_file():
        return None
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        server = config["mcpServers"]["scivo"]
    except (OSError, ValueError, KeyError):
        return None
    env = server.setdefault("env", {})
    checkout, _ = usable_checkout(interpreter or server.get("command"))
    if checkout is None:
        return None
    package = str(checkout / "apps" / "local-mcp")
    if env.get("PYTHONPATH") == package:
        return None
    env["PYTHONPATH"] = package
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return f"this project now runs the MCP from {checkout}"
