"""Updating the MCP, and saying whether it actually moved.

Two traps here, both documented in the project guide because both have bitten
someone:

- An **editable** install reports the version frozen at install time (0.0.1
  forever) and `pip install --upgrade` on it is a no-op that prints success.
  The fix is `git pull` in the checkout, not pip.
- A **git URL** install is only re-fetched when pip thinks the version changed.
  Commits pushed without a version bump look like "already satisfied".

So this asks the interpreter that actually runs the MCP what kind of install it
is, does the right thing for that kind, and then re-reads `git_sha` from a fresh
server process rather than trusting pip's output.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import ScivoConfig

PROBE = r"""
import json, sys, importlib.metadata as md
from pathlib import Path
out = {"found": False}
out["dedicated_venv"] = sys.prefix != sys.base_prefix and not (Path(sys.prefix) / "conda-meta").is_dir()
try:
    dist = md.distribution(sys.argv[1])
    out["found"] = True
    out["version"] = dist.version
    raw = dist.read_text("direct_url.json")
    url = json.loads(raw) if raw else {}
    out["url"] = url.get("url")
    out["subdirectory"] = url.get("subdirectory")
    out["editable"] = bool(url.get("dir_info", {}).get("editable"))
    vcs = url.get("vcs_info") or {}
    out["vcs"] = vcs.get("vcs")
    out["commit_id"] = vcs.get("commit_id")
    out["location"] = str(Path(dist.locate_file("")).resolve())
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""


@dataclass
class Install:
    dist: str = ""
    interpreter: str = ""
    found: bool = False
    version: str = ""
    url: str | None = None
    subdirectory: str | None = None
    editable: bool = False
    dedicated_venv: bool = False
    vcs: str | None = None
    commit_id: str | None = None
    location: str = ""
    error: str | None = None

    @property
    def source_path(self) -> Path | None:
        """Where an editable install's code actually lives.

        `location` is site-packages — where the .pth sits, not the source. The
        source is the file:// URL pip recorded.
        """
        if self.url and self.url.startswith("file://"):
            return Path(self.url[len("file://"):])
        return None

    @property
    def requirement(self) -> str | None:
        """A spec pip can install from.

        `direct_url.json` strips the `git+` prefix and records the VCS in a
        separate field, so handing `url` straight back makes pip treat a repo as
        an archive: "cannot detect archive format". It has to be put back.
        """
        if not self.url:
            return None
        url = f"{self.vcs}+{self.url}" if self.vcs and "+" not in self.url else self.url
        spec = f"{self.dist} @ {url}"
        return f"{spec}#subdirectory={self.subdirectory}" if self.subdirectory else spec

    @property
    def fingerprint(self) -> tuple[str, str]:
        """What "it moved" is judged on. A wheel-built install has no .git, so
        `whoami`'s git_sha is None there — the commit pip recorded is the only
        signal that survives both install kinds."""
        return (self.version, self.commit_id or "")


def _run(argv: list[str], cwd: str | None = None) -> tuple[int, str]:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


def inspect(interpreter: str, dist: str) -> Install:
    """Ask an interpreter how it installed a distribution."""
    code, output = _run([interpreter, "-c", PROBE, dist])
    if code != 0:
        return Install(dist=dist, interpreter=interpreter, error=output[-400:])
    try:
        payload = json.loads(output.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        return Install(dist=dist, interpreter=interpreter,
                       error=f"unreadable probe output: {exc}")
    return Install(dist=dist, interpreter=interpreter, **payload)


def has_remote(root: Path) -> bool:
    code, output = _run(["git", "remote"], cwd=str(root))
    return code == 0 and bool(output.strip())


def repo_root(path: str) -> Path | None:
    """The git checkout an editable install points into."""
    for candidate in [Path(path), *Path(path).parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def apply(install: Install) -> tuple[bool, str]:
    """Perform the update appropriate to how this distribution was installed."""
    if not install.found:
        return False, f"{install.dist} is not installed under {install.interpreter}"

    if install.editable:
        source = install.source_path
        root = repo_root(str(source)) if source else None
        if root is None:
            return False, (f"editable install from {source or install.location}, but no git "
                           "checkout above it — update it however you installed it.")
        if not has_remote(root):
            # The development checkout of the harness itself. Nothing to pull
            # from, and saying "failed" about it would be wrong.
            return True, f"{root} is a local checkout with no remote — nothing to pull"
        code, output = _run(["git", "pull", "--ff-only"], cwd=str(root))
        return code == 0, f"git pull in {root}\n{output}"

    requirement = install.requirement
    if requirement is None:
        return False, (f"installed from an unrecorded source (version {install.version}). "
                       "pip has no direct_url.json to re-fetch from.")
    # --force-reinstall because a git URL whose version did not change reads as
    # "already satisfied"; --no-deps so an MCP update cannot silently move the
    # harness's own pins underneath it.
    code, output = _run([install.interpreter, "-m", "pip", "install", "--upgrade",
                         "--force-reinstall", "--no-deps", requirement])
    return code == 0, output[-1200:]


def checkout_for_mcp() -> Path | None:
    """The source checkout an editable MCP install should point at.

    Same rule the MCP applies when it raises `install_warning`:
    `$CO_SCIENTIST_CHECKOUT`, else the documented clone path.
    """
    import os

    candidates = [os.environ.get("CO_SCIENTIST_CHECKOUT"),
                  str(Path.home() / "co-scientist-mcp-public")]
    for candidate in candidates:
        if candidate and (Path(candidate) / "apps" / "local-mcp").is_dir():
            return Path(candidate)
    return None


def restore_editable(install: Install, checkout: Path) -> tuple[bool, str]:
    """Pull the checkout, then point the interpreter back at it.

    `--no-deps`, because this environment is shared and everything the MCP
    needs is already in it; letting pip re-resolve would risk moving pins other
    projects depend on.
    """
    code, pulled = _run(["git", "pull", "--ff-only"], cwd=str(checkout))
    if code != 0:
        return False, f"git pull in {checkout} failed:\n{pulled}"
    code, output = _run([install.interpreter, "-m", "pip", "install", "-e",
                         str(checkout / "apps" / "local-mcp"), "--no-deps"])
    return code == 0, f"{pulled}\n{output[-600:]}"


def link_skills(config: ScivoConfig) -> tuple[bool, str]:
    code, output = _run([config.command, "-m", "co_scientist_local",
                         "install-skills", "--dir", str(config.root)])
    return code == 0, output[-400:]


def drop_inapplicable_warning(config: ScivoConfig, identity: dict) -> dict:
    """Remove `install_warning` when the MCP runs from a dedicated virtualenv.

    The warning exists for one accident: an editable install in a SHARED
    environment silently replaced by a snapshot, so every project on that
    interpreter runs stale code. A dedicated venv such as ~/.scivo holds a
    snapshot by design and cannot overwrite anyone else's install. The server
    currently raises the warning whenever a clone sits at the default path, so
    in that case it was on every session of every project set up with
    `scivo setup`, telling people to run `pip install -e` for nothing.
    """
    if not identity.get("install_warning"):
        return identity
    install = inspect(config.command, "co-scientist-local")
    if install.found and install.dedicated_venv:
        return {k: v for k, v in identity.items() if k != "install_warning"}
    return identity


def checkout_in_use(config) -> Path | None:
    """The checkout this project's `.mcp.json` actually runs, if it names one."""
    package = (config.env or {}).get("PYTHONPATH", "")
    for entry in package.split(":"):
        candidate = Path(entry)
        if candidate.name == "local-mcp" and (candidate / "co_scientist_local").is_dir():
            return candidate.parent.parent
    return None


def pull_checkout(checkout: Path) -> tuple[bool, str]:
    """`git pull` the clone the session runs from.

    Updating only the pip copy left the session on the old code and the MCP
    kept saying a new version was available — `scivo update` twice over, with
    nothing changing, because the thing being updated was not the thing being
    run.
    """
    before = _run(["git", "rev-parse", "--short", "HEAD"], cwd=str(checkout))[1]
    code, output = _run(["git", "pull", "--ff-only"], cwd=str(checkout))
    if code != 0:
        return False, f"git pull in {checkout} failed:\n{output}"
    after = _run(["git", "rev-parse", "--short", "HEAD"], cwd=str(checkout))[1]
    if before == after:
        return True, f"{checkout} already current ({after})"
    return True, f"pulled {before} → {after}"
