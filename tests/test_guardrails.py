"""The repeat-call breaker, and the one thing it must not break.

The first pytest in this repo (the build was exercised with one-off scratch
scripts; see README "Testing"). `claude_agent_sdk` is stubbed so the rule can
be tested where the SDK is not installed.
"""
from __future__ import annotations

import asyncio
import sys
import types

if "claude_agent_sdk" not in sys.modules:
    stub = types.ModuleType("claude_agent_sdk")
    stub.HookMatcher = lambda *a, **k: None  # type: ignore[attr-defined]
    sys.modules["claude_agent_sdk"] = stub

from scivo_harness import guardrails  # noqa: E402


def _call(g, name, args):
    return asyncio.run(g.on_any_tool({"tool_name": name, "tool_input": args}, None, None))


def test_the_same_read_three_times_in_a_turn_is_blocked():
    g = guardrails.Guardrails()
    a = {"slug": "p", "deck_id": "d", "slide_id": "s1"}
    assert _call(g, "mcp__scivo__preview_slide", a) == {}
    assert _call(g, "mcp__scivo__preview_slide", a) == {}
    out = _call(g, "mcp__scivo__preview_slide", a)
    assert out and "Blocked" in str(out)


def test_a_write_in_between_lets_the_same_read_run_again():
    """update_slide → preview_slide → update_slide → preview_slide, the loop
    /paper-deck requires: each preview follows a write, so none is a repeat."""
    g = guardrails.Guardrails()
    a = {"slug": "p", "deck_id": "d", "slide_id": "s1"}
    for i in range(6):
        assert _call(g, "mcp__scivo__update_slide", {**a, "code": f"v{i}"}) == {}
        assert _call(g, "mcp__scivo__preview_slide", a) == {}, f"blocked on round {i}"


def test_the_same_write_repeated_verbatim_is_still_a_loop():
    g = guardrails.Guardrails()
    a = {"slug": "p", "deck_id": "d", "slide_id": "s1", "code": "same"}
    assert _call(g, "mcp__scivo__update_slide", a) == {}
    assert _call(g, "mcp__scivo__update_slide", a) == {}
    assert "Blocked" in str(_call(g, "mcp__scivo__update_slide", a))


def test_read_prefixes_cover_the_read_only_surface():
    for name in ("mcp__scivo__get_slide", "mcp__scivo__list_papers", "mcp__scivo__preview_slide",
                 "mcp__scivo__lint_manuscript", "mcp__scivo__whoami", "mcp__scivo__project_guide",
                 "mcp__scivo__verify_doi", "mcp__scivo__compare_run_params"):
        assert not guardrails.is_scivo_write(name), name
    for name in ("mcp__scivo__update_slide", "mcp__scivo__add_table", "mcp__scivo__record_decision",
                 "mcp__scivo__export_deck_to_pptx", "mcp__scivo__render_slide"):
        assert guardrails.is_scivo_write(name), name
    assert not guardrails.is_scivo_write("Bash")
