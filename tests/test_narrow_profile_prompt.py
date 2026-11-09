"""A narrow profile must not leave two instructions, one of them impossible.

Under `--profile video` the guide's session-start calls are dropped from the
tool surface, while the guide itself — verbatim in the same prompt — still says
to run them. The session spent calls finding out which existed (feedback
e6f480b9fbe8, a video project with no papers).
"""
from __future__ import annotations

from scivo_harness import toolsets
from scivo_harness.prompt import SESSION_START_TOOLS, _tool_note

NAMES = [
    "whoami", "project_guide", "get_project_memory", "get_project_skills",
    "list_papers", "count_open_user_comments", "list_reviews",
    "review_triage_summary", "check_requirements", "list_servers",
    "get_manuscript", "get_section", "update_section",
    "create_deck", "add_slide", "add_video", "generate_image",
    "submit_remote_job", "record_analysis_run",
]


def test_it_names_the_session_start_calls_this_profile_cannot_make():
    note = _tool_note(toolsets.plan(NAMES, profile="video"))
    assert "ABSENT here" in note
    dropped = set(toolsets.plan(NAMES, profile="video").dropped)
    for name in SESSION_START_TOOLS:
        if name in dropped:
            assert f"`{name}`" in note, name


def test_it_names_none_that_are_actually_present():
    """A note that lists a tool the session HAS is worse than no note: the
    agent stops using something that works."""
    plan = toolsets.plan(NAMES, profile="video")
    note = _tool_note(plan)
    absent = note.split("ABSENT here:", 1)[1].split(".", 1)[0]
    for name in plan.kept:
        assert f"`{name}`" not in absent, name


def test_it_says_the_briefing_already_holds_them():
    # The preflight runs that protocol through its own MCP client, before the
    # tool list is narrowed — so the answers are in this prompt already, and
    # the instruction is to read rather than to call.
    note = _tool_note(toolsets.plan(NAMES, profile="video"))
    assert "briefing" in note
    assert "already holds" in note
    assert "not what" in note and "to do" in note


def test_a_full_profile_says_nothing_about_it():
    # Nothing is absent, so there is nothing to warn about — and `full` must
    # not carry a paragraph that applies to no session.
    assert "ABSENT here" not in _tool_note(toolsets.plan(NAMES, profile="full"))


def test_a_profile_that_keeps_every_session_start_call_says_nothing_either():
    # `paper` drops deck and video tools but none of the protocol's.
    plan = toolsets.plan(NAMES, profile="paper")
    note = _tool_note(plan)
    assert plan.dropped, "this profile does drop something"
    if not [n for n in SESSION_START_TOOLS if n in plan.dropped]:
        assert "ABSENT here" not in note


def test_the_list_is_the_guide_s_own_protocol():
    """Named from the guide, so a reader can check it against the text rather
    than trusting that somebody transcribed it."""
    for expected in ("whoami", "get_project_memory", "list_papers",
                     "count_open_user_comments", "list_servers"):
        assert expected in SESSION_START_TOOLS


def test_the_raw_ssh_refusal_is_actionable_without_a_paper():
    """In a project with no papers, `submit_remote_job(slug=…)` read as
    inapplicable and the session went looking for a detour."""
    from scivo_harness.guardrails import Guardrails
    import asyncio

    rails = Guardrails()
    payload = {"tool_input": {"command": "ssh node2 \"nohup python gen.py &\""}}
    out = asyncio.run(rails.on_bash(payload, None, None))
    text = str(out)
    assert "_project" in text, "the paper-less path has to be named here"
    assert "does not need" in text and "manuscript" in text
