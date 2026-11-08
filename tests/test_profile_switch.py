"""`/profile` — narrow or widen the tool surface without losing the conversation.

The profile is `allowed_tools` / `disallowed_tools` on the options, and the CLI
reads those when it starts, so switching means a new client. Unlike a provider
switch it RESUMES: same endpoint, same model, nothing about the history the new
process cannot take, and the whole point of switching mid-session is to carry on
with a different kit (user, 2026-11-08).
"""
from __future__ import annotations

import asyncio
import types

import pytest

from scivo_harness import toolsets
from scivo_harness.repl import ANSI, Repl

TOOLS = (
    "create_paper", "add_section", "update_section", "add_figure",
    "add_material", "get_material", "list_materials",
    "submit_remote_job", "record_analysis_run", "list_analysis_runs",
    "create_deck", "add_slide", "render_deck",
    "add_video", "list_videos",
)


class FakeClient:
    def __init__(self, options=None, fail: bool = False):
        self.options = options
        self.fail = fail
        self.connected = False
        self.mode = None

    async def connect(self):
        if self.fail:
            raise RuntimeError("the CLI would not start")
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def set_permission_mode(self, mode):
        self.mode = mode


def _repl(profile="full", session_id="sess-1", read_only=False, fail_connect=False):
    plan = toolsets.plan(list(TOOLS), profile=profile, read_only=read_only)
    options = types.SimpleNamespace(permission_mode="default", allowed_tools=[],
                                    disallowed_tools=[], resume=None)
    built: list[dict] = []

    def options_for(provider, **changes):
        built.append(dict(changes))
        return types.SimpleNamespace(permission_mode="default", **changes)

    from scivo_harness.preflight import Briefing

    session = types.SimpleNamespace(
        plan=plan, options=options, full_options=options, scivo_tools=TOOLS,
        # The append is rebuilt on every switch, so the switch needs both of
        # these — the briefing to build from and the guide to put back.
        briefing=Briefing(),
        guide="PROJECT GUIDE TEXT",
        provider=types.SimpleNamespace(name="anthropic", lean_profile=False),
        model="claude-opus-5", options_for=options_for,
    )
    stub = types.SimpleNamespace(session=session, session_id=session_id,
                                 mode="default", control=None, said=[],
                                 built=built, clients=[])
    stub.client = FakeClient(options)
    stub._say = stub.said.append
    stub._stop_pump = lambda: asyncio.sleep(0)
    stub._start_pump = lambda: asyncio.sleep(0)
    for name in ("_profile", "_profile_rows"):
        setattr(stub, name, getattr(Repl, name).__get__(stub))
    import scivo_harness.repl as repl_mod
    # Only the FIRST new client fails. The fallback — the one that puts the old
    # options back — has to be able to connect, or the test is measuring a
    # session that cannot reconnect at all rather than the restore path.
    remaining = [fail_connect]

    def factory(options=None):
        fail = bool(remaining and remaining.pop(0))
        return FakeClient(options, fail=fail)
    repl_mod.ClaudeSDKClient = factory
    return stub


def _text(stub) -> str:
    return ANSI.sub("", "\n".join(stub.said))


def test_the_menu_numbers_every_profile_and_marks_the_current_one():
    stub = _repl(profile="writing")
    asyncio.run(stub._profile(""))
    said = _text(stub)
    for name in toolsets.PROFILES:
        assert name in said, name
    assert "← this session" in said
    # The count is this server's own tools, not a number from the source.
    kept = len(toolsets.plan(list(TOOLS), profile="writing").kept)
    assert f"{kept} tools" in said


def test_the_menu_says_skills_are_not_scoped_by_it():
    """Asked as "mcp 나 스킬 쓰는 범위", and only one of the two is true."""
    stub = _repl()
    asyncio.run(stub._profile(""))
    assert "skills stay available in every profile" in _text(stub)


def test_a_number_picks_from_the_list_that_printed():
    stub = _repl(profile="full")
    rows = stub._profile_rows()
    target = next(i for i, (name, _, _) in enumerate(rows, start=1) if name == "deck")
    asyncio.run(stub._profile(str(target)))
    assert stub.session.plan.profile == "deck"


def test_switching_resumes_the_same_conversation():
    stub = _repl(profile="full", session_id="sess-1")
    asyncio.run(stub._profile("analysis"))
    assert stub.built, "the options were never rebuilt"
    assert stub.built[-1]["resume"] == "sess-1"
    assert stub.built[-1]["continue_conversation"] is False
    assert "the conversation continues" in _text(stub)


def test_it_says_so_when_there_is_no_conversation_to_resume():
    """Worth knowing BEFORE typing the next message into an empty one."""
    stub = _repl(profile="full", session_id=None)
    asyncio.run(stub._profile("analysis"))
    assert stub.built[-1]["resume"] is None
    assert "NEW conversation" in _text(stub)


def test_the_new_kit_is_what_the_profile_asks_for():
    stub = _repl(profile="full")
    asyncio.run(stub._profile("deck"))
    disallowed = stub.built[-1]["disallowed_tools"]
    # A deck session keeps deck + materials and drops the paper tools.
    assert any(name.endswith("create_deck") for name in
               stub.session.plan.disallowed) is False
    assert any(name.endswith("create_paper") for name in disallowed)
    assert all(not name.endswith("add_slide") for name in disallowed)


def test_the_host_tools_stay_approved_across_a_switch():
    from scivo_harness.session import AUTO_APPROVED_HOST_TOOLS

    stub = _repl(profile="full")
    asyncio.run(stub._profile("analysis"))
    allowed = stub.built[-1]["allowed_tools"]
    for tool in AUTO_APPROVED_HOST_TOOLS:
        assert tool in allowed, tool


def test_a_read_only_session_stays_read_only():
    stub = _repl(profile="full", read_only=True)
    asyncio.run(stub._profile("paper"))
    assert stub.session.plan.read_only_session is True
    assert "read-only session" in _text(stub)


def test_the_same_profile_is_a_no_op():
    stub = _repl(profile="paper")
    asyncio.run(stub._profile("paper"))
    assert "already on paper" in _text(stub)
    assert not stub.built, "it must not rebuild the client for nothing"


def test_an_unknown_name_lists_the_real_ones_and_changes_nothing():
    stub = _repl(profile="full")
    asyncio.run(stub._profile("nonsense"))
    assert "no profile called" in _text(stub)
    assert stub.session.plan.profile == "full"
    assert not stub.built


def test_a_number_out_of_range_changes_nothing():
    stub = _repl(profile="full")
    asyncio.run(stub._profile("99"))
    assert "there is no choice 99" in _text(stub)
    assert stub.session.plan.profile == "full"


def test_a_client_that_will_not_start_puts_the_old_profile_back():
    """The plan is set before the options are built, so a failed connect must
    restore it — otherwise the session reports a kit it is not running."""
    stub = _repl(profile="full", fail_connect=True)
    asyncio.run(stub._profile("analysis"))
    assert stub.session.plan.profile == "full", "the plan was left lying"
    said = _text(stub)
    assert "could not switch profile" in said and "still on full" in said


def _append_of(stub) -> str:
    return stub.built[-1]["system_prompt"]["append"]


def test_the_system_prompt_follows_the_profile():
    """The append names the profile and counts its tools.

    Left alone across a switch it would tell the model it is running the kit
    it had a moment ago — the model's own description of its tools would be
    wrong while the tools themselves were right.
    """
    stub = _repl(profile="full")
    asyncio.run(stub._profile("analysis"))
    append = _append_of(stub)
    assert "`analysis` profile" in append
    assert "`full` profile" not in append


def test_none_drops_the_guide_as_well_as_the_tools():
    """Both guides are instructions for calling tools that are now absent."""
    stub = _repl(profile="full")
    asyncio.run(stub._profile("none"))
    append = _append_of(stub)
    assert "PROJECT GUIDE TEXT" not in append
    assert "carries no scivo tools" in append
    assert "/profile full" in append, "it has to say how to get them back"
    assert stub.built[-1]["disallowed_tools"], "and every tool is gone"
    assert stub.built[-1]["allowed_tools"] == [
        *__import__("scivo_harness.session", fromlist=["x"]).AUTO_APPROVED_HOST_TOOLS,
    ], "only the host's own tools remain approved"


def test_switching_back_from_none_restores_the_guide():
    stub = _repl(profile="none")
    asyncio.run(stub._profile("full"))
    append = _append_of(stub)
    assert "PROJECT GUIDE TEXT" in append
    assert "carries no scivo tools" not in append


def test_none_keeps_the_conversation_too():
    stub = _repl(profile="full", session_id="sess-9")
    asyncio.run(stub._profile("none"))
    assert stub.built[-1]["resume"] == "sess-9"
    assert "the conversation continues" in _text(stub)


def test_none_is_in_the_menu_and_says_it_carries_nothing():
    stub = _repl(profile="full")
    asyncio.run(stub._profile(""))
    said = _text(stub)
    assert "none" in said
    assert "  0 tools" in said
