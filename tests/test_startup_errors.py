"""What the harness shows when the MCP server never comes up.

A server that dies before answering `initialize` raises from inside two nested
anyio task groups, so what reached the user was a 60-line ExceptionGroup ending
in "McpError: Connection closed" — the transport describing the symptom, with
the cause (one line on the server's stderr) in a file nobody printed. A real
`scivo setup` run ended that way over a mistyped key, and the config it had
already written stayed behind.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from scivo_harness.config import load
from scivo_harness.preflight import ServerUnavailable
from scivo_harness.scivo_mcp import _startup_failure, connect
from scivo_harness.setup import interpreter_path


class _Cfg:
    command = "/usr/bin/python3"
    args = ["-m", "co_scientist_local"]


def test_the_servers_own_words_lead():
    """It knows why — "the project API key was refused…" — so that is the
    message, and the transport error is not even mentioned."""
    msg = _startup_failure(
        _Cfg(), "co-scientist-local: the project API key was refused\n", RuntimeError("x"))
    assert "did not start" in msg
    assert "the project API key was refused" in msg
    assert "RuntimeError" not in msg


def test_silence_is_its_own_diagnosis():
    """No stderr at all means the process never got far enough to speak, which
    almost always means the command is not an interpreter that has the
    package — so say which command, and how to check it."""
    msg = _startup_failure(_Cfg(), "", RuntimeError("boom"))
    assert "said nothing on stderr" in msg
    assert "/usr/bin/python3 -m co_scientist_local" in msg
    assert "RuntimeError" in msg


def test_a_dead_command_raises_server_unavailable_not_an_exception_group(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"scivo": {
        "command": "/bin/false", "args": [], "env": {"CO_SCIENTIST_API_KEY": "csk_x"}}}}))
    config = load(tmp_path)

    async def go():
        async with connect(config):
            pass

    with pytest.raises(ServerUnavailable) as caught:
        asyncio.run(go())
    assert "did not start" in str(caught.value)


def test_an_error_inside_the_session_is_not_relabelled(tmp_path, monkeypatch):
    """Only STARTUP is translated. Once the session is live, an exception
    belongs to the caller's body and has to pass through as itself."""
    import scivo_harness.scivo_mcp as mod

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def initialize(self): return None

    class _Stdio:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return (None, None)
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(mod, "stdio_client", lambda *a, **k: _Stdio())
    monkeypatch.setattr(mod, "ClientSession", lambda *a, **k: _Session())
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"scivo": {
        "command": "/bin/true", "args": [], "env": {}}}}))
    config = load(tmp_path)

    async def go():
        async with connect(config):
            raise ZeroDivisionError("mine")

    with pytest.raises(ZeroDivisionError):
        asyncio.run(go())


# ── the interpreter the user names is the one that gets written ─────────────

def test_a_venv_interpreter_keeps_its_symlinks(tmp_path):
    """`Path.resolve()` was here, and it undid what a venv is for:
    `~/.scivo/bin/python` is a symlink to `bin/python3.11`, itself a symlink to
    the base interpreter, so resolving wrote `/…/miniconda3/bin/python3.11`
    into .mcp.json. That interpreter runs whatever copy of the package someone
    else installed — on the machine where this was found, an editable install
    of a two-month-old checkout, which then served the project.
    """
    base = tmp_path / "usr" / "bin"
    base.mkdir(parents=True)
    real = base / "python3.11"
    real.write_text("#!/bin/sh\n")
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python3.11").symlink_to(real)
    (venv / "python").symlink_to("python3.11")

    kept = interpreter_path(str(venv / "python"))
    assert kept == str(venv / "python")
    assert "usr/bin" not in kept


def test_it_still_makes_a_relative_path_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "py").write_text("#!/bin/sh\n")
    assert interpreter_path("./py") == str(tmp_path / "py")
    assert os.path.isabs(interpreter_path("~/nothing/here"))
