"""`--read-only` means the session cannot change files, not only the dashboard.

It removed the scivo tools that write and left the host's Write and Edit
loaded, in a session whose prompt told the model "every writing tool is
absent" (review 2026-11-10).
"""
from __future__ import annotations

from scivo_harness import prompt, toolsets
from scivo_harness.preflight import Briefing

TOOLS = ["get_section", "update_section", "add_figure", "list_papers", "whoami"]


def test_the_hosts_file_tools_are_dropped_with_the_scivo_writers():
    plan = toolsets.plan(TOOLS, read_only=True)
    for tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        assert tool in plan.disallowed, tool
    assert "mcp__scivo__update_section" in plan.disallowed
    # Bash stays: reading a log needs it. It is asked about every time.
    assert "Bash" not in plan.disallowed


def test_an_ordinary_session_keeps_them():
    plan = toolsets.plan(TOOLS)
    assert not any(tool in plan.disallowed for tool in toolsets.HOST_FILE_WRITERS)


def test_a_profile_switch_cannot_hand_them_back():
    """`/profile` rebuilds the options from the plan. The list lives on the
    plan so the rebuilt session is read-only too, in every profile."""
    for profile in toolsets.PROFILES:
        plan = toolsets.plan(TOOLS, profile=profile, read_only=True)
        assert "Write" in plan.disallowed and "Edit" in plan.disallowed, profile


def test_the_prompt_says_what_is_actually_absent_and_what_is_not():
    brief = Briefing(identity={"project_id": "p"})
    text = prompt.build(brief, toolsets.plan(TOOLS, read_only=True), guide=None)
    assert "read-only session" in text
    assert "Write, Edit" in text
    assert "Bash is still here" in text and "asked about every command" in text
    # The sentence that was false:
    assert "every writing tool is absent" not in text


def test_a_read_only_session_with_no_scivo_tools_still_says_so():
    brief = Briefing(identity={"project_id": "p"})
    text = prompt.build(brief, toolsets.plan(TOOLS, profile="none", read_only=True), guide=None)
    assert "read-only session" in text and "Write, Edit" in text


def test_the_flag_describes_what_it_does():
    from scivo_harness import cli
    text = " ".join(cli._parser().format_help().split())
    assert "Write, Edit" in text and "Bash stays" in text
