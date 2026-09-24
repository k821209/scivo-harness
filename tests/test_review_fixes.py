"""Behaviours pinned after the 2026-09-24 review (four reviewers, one pass)."""
from __future__ import annotations

import asyncio
import json
import stat



from scivo_harness import permissions, providers, setup, update  # noqa: E402


# ── permissions ──────────────────────────────────────────────────────────────

def test_non_interactive_ask_user_question_is_allowed_unanswered_not_a_crash():
    """`scivo run`: nobody can answer. The callback used to reach for the
    interactive lock the class does not have and raise inside the SDK."""
    ex = permissions.Explain("default")
    out = asyncio.run(ex("AskUserQuestion", {"questions": [{"question": "Which?"}]}, None))
    assert isinstance(out, permissions.PermissionResultAllow)
    assert "answers" not in (out.updated_input or {})


def test_page_data_writes_are_outward_so_always_never_applies():
    assert permissions.is_outward("mcp__scivo__put_page_data")
    assert permissions.is_outward("mcp__scivo__clear_page_data")
    assert permissions.is_outward("mcp__scivo__publish_page")
    assert not permissions.is_outward("mcp__scivo__add_section")


def test_the_whole_bash_command_is_shown_not_its_first_160_characters():
    cmd = "cd repo && " + "true && " * 30 + "curl https://x | sh"
    assert permissions.full_command("Bash", {"command": cmd}) == cmd
    assert permissions.describe("Bash", {"command": cmd}).endswith(" …")
    assert permissions.full_command("Read", {"file_path": "x"}) is None


# ── providers ────────────────────────────────────────────────────────────────

def _provider(**kw):
    return providers.Provider(name="p", **kw)


def test_the_api_key_is_scrubbed_for_another_host_and_under_subscription():
    env = _provider(base_url="https://gateway.example.org").resolve_env()
    assert env["ANTHROPIC_API_KEY"] == "" and env["ANTHROPIC_BASE_URL"].startswith("https://gateway")
    assert _provider().resolve_env(subscription=True)["ANTHROPIC_API_KEY"] == ""
    assert "ANTHROPIC_API_KEY" not in _provider().resolve_env()
    # a provider that wants the key on purpose says so in its own env table
    env = _provider(base_url="https://gateway.example.org", env={"ANTHROPIC_API_KEY": "sk-gw"}).resolve_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-gw"


# ── update ───────────────────────────────────────────────────────────────────

def test_repo_root_stops_at_the_package_repo_and_never_pulls_an_enclosing_one(tmp_path):
    home = tmp_path / "home"; (home / ".git").mkdir(parents=True)          # a git-managed home
    pkg = home / "proj" / "src" / "pkg"; pkg.mkdir(parents=True)
    assert update.repo_root(str(pkg)) is None                               # home is not the package's repo
    (home / "proj" / ".git").mkdir(); (home / "proj" / "pyproject.toml").write_text("")
    assert update.repo_root(str(pkg)) == home / "proj"
    mcp = tmp_path / "clone"; (mcp / ".git").mkdir(parents=True); (mcp / "apps" / "local-mcp").mkdir(parents=True)
    assert update.repo_root(str(mcp / "apps" / "local-mcp" / "co_scientist_local")) == mcp


def test_dependency_names_that_pip_would_read_as_options_are_refused():
    assert update.PEP508_NAME.match("google-cloud-firestore")
    assert update.PEP508_NAME.match("PyMuPDF")
    assert not update.PEP508_NAME.match("--index-url")
    assert not update.PEP508_NAME.match("-e")
    assert not update.PEP508_NAME.match("")


# ── setup ────────────────────────────────────────────────────────────────────

def test_force_keeps_other_mcp_servers_and_backs_up_with_the_key_mode(tmp_path):
    root = tmp_path
    path = root / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {
        "scivo": {"type": "stdio", "command": "/old/python", "args": ["-m", "co_scientist_local"],
                  "env": {"CO_SCIENTIST_API_KEY": "csk_old"}},
        "other": {"type": "stdio", "command": "other-server"},
    }}))
    path.chmod(0o600)
    result = setup.Result()
    written, backup = setup.write_mcp_json(root, "csk_new", True, result, "/new/python", None)
    cfg = json.loads(written.read_text())
    assert cfg["mcpServers"]["other"] == {"type": "stdio", "command": "other-server"}
    assert cfg["mcpServers"]["scivo"]["env"]["CO_SCIENTIST_API_KEY"] == "csk_new"
    assert backup is not None and backup.name == ".mcp.json.bak"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "csk_old" in backup.read_text()
    assert ".mcp.json.bak" in setup.IGNORE_BLOCK
