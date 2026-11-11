"""Skill invocations reach the same local tally the MCP writes tool calls to,
and the harness's own MCP connections say they are the harness's."""
from __future__ import annotations

import asyncio

import pytest

from scivo_harness import guardrails, usage


@pytest.fixture(autouse=True)
def _dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SCIVO_USAGE_DIR", str(tmp_path / "usage"))
    monkeypatch.delenv("SCIVO_USAGE", raising=False)
    return tmp_path / "usage"


def _rows(folder):
    return [line.split("\t") for p in sorted(folder.glob("*.log")) for line in p.read_text().splitlines()]


def test_a_skill_the_model_invokes_is_counted(_dir):
    rails = guardrails.Guardrails(root=None)
    rails.project_id = "proj-1"
    asyncio.run(rails.on_tool_success(
        {"tool_name": "Skill", "tool_input": {"skill": "paper-writing", "args": "the methods"}}, None, None))
    asyncio.run(rails.on_tool_success({"tool_name": "Read", "tool_input": {"file_path": "/x"}}, None, None))
    rows = _rows(_dir)
    assert [r[1:4] for r in rows] == [["skill", "paper-writing", "proj-1"]]
    assert "methods" not in "".join(p.read_text() for p in _dir.glob("*.log"))   # the name, nothing else


def test_a_skill_the_user_types_is_recognised_and_other_slashes_are_not():
    skills = {"paper-writing", "paper-deck"}
    assert usage.typed_skill("/paper-writing the methods section", skills) == "paper-writing"
    assert usage.typed_skill("  /paper-deck", skills) == "paper-deck"
    for line in ("/model fable", "/tmp/figure.png 보여줘", "/", "write /paper-writing", "paper-writing", ""):
        assert usage.typed_skill(line, skills) is None, line


def test_off_writes_nothing(_dir, monkeypatch):
    monkeypatch.setenv("SCIVO_USAGE", "off")
    usage.record_skill("paper-writing", "p")
    assert not _dir.exists()


def test_a_tally_that_cannot_be_written_does_not_raise(tmp_path, monkeypatch):
    blocked = tmp_path / "a-file"
    blocked.write_text("x")
    monkeypatch.setenv("SCIVO_USAGE_DIR", str(blocked / "usage"))
    usage.record_skill("paper-writing", "p")


def test_the_harness_marks_its_own_connections():
    """Its briefing is ten calls at every start; unmarked, they would be the
    model's ten most-used tools in the report."""
    import inspect
    from scivo_harness import scivo_mcp
    assert '"SCIVO_USAGE_AS": "harness"' in inspect.getsource(scivo_mcp.connect)
