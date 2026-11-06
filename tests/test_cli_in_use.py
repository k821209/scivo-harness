"""WHICH Claude Code a session runs — the question `claude --version` answers wrong.

The SDK spawns the copy bundled inside claude_agent_sdk BEFORE anything on
PATH (`_find_cli` → `_find_bundled_cli`). So a user can run `claude update`,
be told it succeeded, and hit the identical "version 2.1.280 or newer is
required" on the next turn, with the error's own two remedies both spent and
nothing left to try (user, 2026-11-06). `scivo update` does not move it
either: the pin is a floor and the dep top-up installs only packages that are
MISSING.
"""
from __future__ import annotations

import sys
from pathlib import Path

from scivo_harness import failures


def _bundle(tmp_path: Path, version: str) -> Path:
    """A fake SDK package whose _bundled/claude prints `version`."""
    pkg = tmp_path / "claude_agent_sdk"
    (pkg / "_bundled").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    cli = pkg / "_bundled" / "claude"
    cli.write_text(f"#!/bin/sh\necho '{version} (Claude Code)'\n")
    cli.chmod(0o755)
    return cli


def _fake_sdk(monkeypatch, tmp_path: Path, version: str) -> Path:
    cli = _bundle(tmp_path, version)
    import types
    module = types.ModuleType("claude_agent_sdk")
    module.__file__ = str(tmp_path / "claude_agent_sdk" / "__init__.py")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return cli


def test_the_bundled_copy_wins_over_path(monkeypatch, tmp_path):
    bundled = _fake_sdk(monkeypatch, tmp_path, "2.1.274")
    monkeypatch.setattr(failures.shutil, "which", lambda _n: "/usr/bin/claude")
    assert failures.cli_in_use() == bundled          # NOT the one on PATH
    assert failures.cli_version(bundled) == "2.1.274"


def test_path_is_used_when_the_sdk_carries_nothing(monkeypatch, tmp_path):
    import types
    module = types.ModuleType("claude_agent_sdk")
    module.__file__ = str(tmp_path / "nothing" / "__init__.py")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    monkeypatch.setattr(failures.shutil, "which", lambda _n: "/usr/bin/claude")
    assert failures.bundled_cli() is None
    assert failures.cli_in_use() == Path("/usr/bin/claude")
    # And then there is nothing to be behind: that copy IS the one in use.
    assert failures.cli_outpaced() == ("ok", None)


def test_it_says_so_when_claude_update_outpaced_the_bundled_copy(monkeypatch, tmp_path):
    _fake_sdk(monkeypatch, tmp_path, "2.1.274")
    newer = tmp_path / "path-claude"
    newer.write_text("#!/bin/sh\necho '2.1.316 (Claude Code)'\n")
    newer.chmod(0o755)
    monkeypatch.setattr(failures.shutil, "which", lambda _n: str(newer))
    state, note = failures.cli_outpaced()
    assert state == "outpaced"
    assert note and "2.1.316" in note and "2.1.274" in note
    assert "pip install -U claude-agent-sdk" in note
    assert "restart" in note


def test_it_stays_quiet_when_the_bundled_copy_is_current(monkeypatch, tmp_path):
    _fake_sdk(monkeypatch, tmp_path, "2.1.316")
    same = tmp_path / "path-claude"
    same.write_text("#!/bin/sh\necho '2.1.316 (Claude Code)'\n")
    same.chmod(0o755)
    monkeypatch.setattr(failures.shutil, "which", lambda _n: str(same))
    assert failures.cli_outpaced() == ("ok", None)


def test_an_unreadable_version_is_its_own_answer(monkeypatch, tmp_path):
    """"could not compare" must be distinguishable from "compared, fine".

    The first version returned None for both, so a caller could not tell a
    passing check from one that never ran — and `scivo doctor` printed
    nothing, which reads as a pass.
    """
    _fake_sdk(monkeypatch, tmp_path, "2.1.274")
    mute = tmp_path / "path-claude"
    mute.write_text("#!/bin/sh\nexit 1\n")
    mute.chmod(0o755)
    monkeypatch.setattr(failures.shutil, "which", lambda _n: str(mute))
    assert failures.cli_version(mute) is None
    state, why = failures.cli_outpaced()
    assert state == "unknown"
    assert why and str(mute) in why                   # names what it could not read
    assert state != "ok"


def test_the_version_error_names_the_binary_that_is_actually_running(monkeypatch, tmp_path):
    bundled = _fake_sdk(monkeypatch, tmp_path, "2.1.274")
    monkeypatch.setattr(failures.shutil, "which", lambda _n: None)
    said = failures.explain(RuntimeError(
        "API Error: 400 Claude Code 2.1.274 does not support this model; "
        "version 2.1.280 or newer is required. Run 'claude update', or update "
        "the Claude desktop app, then try again."))
    assert said is not None
    assert str(bundled) in said and "2.1.274" in said
    # The error's own advice is named as NOT the lever, and the real one given.
    assert "claude update" in said and "does not reach it" in said
    assert f"{sys.executable} -m pip install -U claude-agent-sdk" in said
    assert "/model" in said


def test_an_ordinary_failure_is_not_mistaken_for_this_one(monkeypatch, tmp_path):
    _fake_sdk(monkeypatch, tmp_path, "2.1.274")
    # Both markers are required: "or newer is required" alone shows up in
    # unrelated dependency complaints.
    assert failures.explain(RuntimeError("numpy>=2 or newer is required")) is None
