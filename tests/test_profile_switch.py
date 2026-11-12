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
from dataclasses import dataclass, field
from typing import Any

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


@dataclass
class Opts:
    """Enough of ClaudeAgentOptions to be `dataclasses.replace`d.

    A real dataclass on purpose: `options_rekitted` works by replacing fields
    on the options THIS session runs, and the property under test is that
    `env` and `model` come through untouched.
    """
    env: dict
    model: str
    system_prompt: Any = None
    allowed_tools: list = field(default_factory=list)
    disallowed_tools: list = field(default_factory=list)
    resume: str | None = None
    continue_conversation: bool = False
    tools: list | None = None
    permission_mode: str = "default"


LOCAL_ENV = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8099",
             "ANTHROPIC_AUTH_TOKEN": "local-token"}


def _repl(monkeypatch, profile="full", session_id="sess-1", read_only=False,
          fail_connect=False, local=False, lean=False):
    from scivo_harness.preflight import Briefing
    from scivo_harness.session import Session

    plan = toolsets.plan(list(TOOLS), profile=profile, read_only=read_only)
    # What the session is RUNNING: the local endpoint when asked for.
    options = Opts(env=dict(LOCAL_ENV) if local else {"MCP_DISCOVERY_CACHE": "false"},
                   model="qwen3-27b" if local else "claude-opus-5")
    # What `full_options` is: the Claude-shaped snapshot from launch, which
    # `/model` deliberately does not update when it switches to a local
    # endpoint. Building a profile from it is the bug.
    full_options = Opts(env={"MCP_DISCOVERY_CACHE": "false"}, model="claude-opus-5")

    session = types.SimpleNamespace(
        plan=plan, options=options, full_options=full_options, scivo_tools=TOOLS,
        briefing=Briefing(), guide="PROJECT GUIDE TEXT", lean_append="LAUNCH LEAN APPEND",
        provider=types.SimpleNamespace(name="local" if local else "anthropic",
                                       lean_profile=lean),
        model=options.model, lean_tools=None,
    )
    from scivo_harness.guardrails import Guardrails
    session.rails = Guardrails()
    session.kit = Session.kit.__get__(session)
    # The REAL methods, both of them, so a falsification that swaps one for
    # the other fails on what it actually got wrong — the endpoint and the
    # model — rather than on a stub that happens to be missing a name.
    session.options_rekitted = Session.options_rekitted.__get__(session)
    session.options_for = Session.options_for.__get__(session)

    stub = types.SimpleNamespace(session=session, session_id=session_id,
                                 mode="default", control=None, said=[],
                                 clients=[])
    stub.client = FakeClient(options)
    stub._say = stub.said.append
    stub._stop_pump = lambda: asyncio.sleep(0)
    stub._start_pump = lambda: asyncio.sleep(0)
    stub._swap_client = Repl._swap_client.__get__(stub)
    stub._resumed = Repl._resumed.__get__(stub)
    for name in ("_profile", "_profile_rows"):
        setattr(stub, name, getattr(Repl, name).__get__(stub))
    import scivo_harness.repl as repl_mod
    remaining = [fail_connect]

    def factory(options=None):
        fail = bool(remaining and remaining.pop(0))
        client = FakeClient(options, fail=fail)
        stub.clients.append(client)
        return client
    # Through monkeypatch, so it is put back: assigning the module attribute
    # directly leaks this factory into every later test module that builds a
    # client, and the damage shows up only in a full run.
    monkeypatch.setattr(repl_mod, "ClaudeSDKClient", factory)
    return stub


def _built(stub):
    """The options the new client was constructed with."""
    return stub.clients[-1].options


def _text(stub) -> str:
    return ANSI.sub("", "\n".join(stub.said))


def test_the_menu_numbers_every_profile_and_marks_the_current_one(monkeypatch):
    stub = _repl(monkeypatch, profile="writing")
    asyncio.run(stub._profile(""))
    said = _text(stub)
    for name in toolsets.PROFILES:
        assert name in said, name
    assert "← this session" in said
    # The count is this server's own tools, not a number from the source.
    kept = len(toolsets.plan(list(TOOLS), profile="writing").kept)
    assert f"{kept} tools" in said


def test_the_menu_says_skills_are_not_scoped_by_it(monkeypatch):
    """Asked as "mcp 나 스킬 쓰는 범위", and only one of the two is true."""
    stub = _repl(monkeypatch, )
    asyncio.run(stub._profile(""))
    assert "skills stay available in every profile" in _text(stub)


def test_a_number_picks_from_the_list_that_printed(monkeypatch):
    stub = _repl(monkeypatch, profile="full")
    rows = stub._profile_rows()
    target = next(i for i, (name, _, _) in enumerate(rows, start=1) if name == "deck")
    asyncio.run(stub._profile(str(target)))
    assert stub.session.plan.profile == "deck"


def test_switching_resumes_the_same_conversation(monkeypatch):
    stub = _repl(monkeypatch, profile="full", session_id="sess-1")
    asyncio.run(stub._profile("analysis"))
    assert stub.clients, "the client was never rebuilt"
    assert _built(stub).resume == "sess-1"
    assert _built(stub).continue_conversation is False
    assert "the conversation continues" in _text(stub)


def test_it_says_so_when_there_is_no_conversation_to_resume(monkeypatch):
    """Worth knowing BEFORE typing the next message into an empty one."""
    stub = _repl(monkeypatch, profile="full", session_id=None)
    asyncio.run(stub._profile("analysis"))
    assert _built(stub).resume is None
    assert "NEW conversation" in _text(stub)


def test_the_new_kit_is_what_the_profile_asks_for(monkeypatch):
    stub = _repl(monkeypatch, profile="full")
    asyncio.run(stub._profile("deck"))
    disallowed = _built(stub).disallowed_tools
    # A deck session keeps deck + materials and drops the paper tools.
    assert any(name.endswith("create_deck") for name in
               stub.session.plan.disallowed) is False
    assert any(name.endswith("create_paper") for name in disallowed)
    assert all(not name.endswith("add_slide") for name in disallowed)


def test_the_host_tools_stay_approved_across_a_switch(monkeypatch):
    from scivo_harness.session import AUTO_APPROVED_HOST_TOOLS

    stub = _repl(monkeypatch, profile="full")
    asyncio.run(stub._profile("analysis"))
    allowed = _built(stub).allowed_tools
    for tool in AUTO_APPROVED_HOST_TOOLS:
        assert tool in allowed, tool


def test_a_read_only_session_stays_read_only(monkeypatch):
    stub = _repl(monkeypatch, profile="full", read_only=True)
    asyncio.run(stub._profile("paper"))
    assert stub.session.plan.read_only_session is True
    assert "read-only session" in _text(stub)


def test_the_same_profile_is_a_no_op(monkeypatch):
    stub = _repl(monkeypatch, profile="paper")
    asyncio.run(stub._profile("paper"))
    assert "already on paper" in _text(stub)
    assert not stub.clients, "it must not rebuild the client for nothing"


def test_an_unknown_name_lists_the_real_ones_and_changes_nothing(monkeypatch):
    stub = _repl(monkeypatch, profile="full")
    asyncio.run(stub._profile("nonsense"))
    assert "no profile called" in _text(stub)
    assert stub.session.plan.profile == "full"
    assert not stub.clients


def test_a_number_out_of_range_changes_nothing(monkeypatch):
    stub = _repl(monkeypatch, profile="full")
    asyncio.run(stub._profile("99"))
    assert "there is no choice 99" in _text(stub)
    assert stub.session.plan.profile == "full"


def test_a_client_that_will_not_start_puts_the_old_profile_back(monkeypatch):
    """The plan is set before the options are built, so a failed connect must
    restore it — otherwise the session reports a kit it is not running."""
    stub = _repl(monkeypatch, profile="full", fail_connect=True)
    asyncio.run(stub._profile("analysis"))
    assert stub.session.plan.profile == "full", "the plan was left lying"
    said = _text(stub)
    assert "could not switch profile" in said and "still on full" in said


def _append_of(stub) -> str:
    return _built(stub).system_prompt["append"]


def test_the_system_prompt_follows_the_profile(monkeypatch):
    """The append names the profile and counts its tools.

    Left alone across a switch it would tell the model it is running the kit
    it had a moment ago — the model's own description of its tools would be
    wrong while the tools themselves were right.
    """
    stub = _repl(monkeypatch, profile="full")
    asyncio.run(stub._profile("analysis"))
    append = _append_of(stub)
    assert "`analysis` profile" in append
    assert "`full` profile" not in append


def test_none_drops_the_guide_as_well_as_the_tools(monkeypatch):
    """Both guides are instructions for calling tools that are now absent."""
    stub = _repl(monkeypatch, profile="full")
    asyncio.run(stub._profile("none"))
    append = _append_of(stub)
    assert "PROJECT GUIDE TEXT" not in append
    assert "carries no scivo tools" in append
    assert "/profile full" in append, "it has to say how to get them back"
    assert _built(stub).disallowed_tools, "and every tool is gone"
    assert _built(stub).allowed_tools == [
        *__import__("scivo_harness.session", fromlist=["x"]).AUTO_APPROVED_HOST_TOOLS,
    ], "only the host's own tools remain approved"


def test_switching_back_from_none_restores_the_guide(monkeypatch):
    stub = _repl(monkeypatch, profile="none")
    asyncio.run(stub._profile("full"))
    append = _append_of(stub)
    assert "PROJECT GUIDE TEXT" in append
    assert "carries no scivo tools" not in append


def test_none_keeps_the_conversation_too(monkeypatch):
    stub = _repl(monkeypatch, profile="full", session_id="sess-9")
    asyncio.run(stub._profile("none"))
    assert _built(stub).resume == "sess-9"
    assert "the conversation continues" in _text(stub)


def test_none_is_in_the_menu_and_says_it_carries_nothing(monkeypatch):
    stub = _repl(monkeypatch, profile="full")
    asyncio.run(stub._profile(""))
    said = _text(stub)
    assert "none" in said
    assert "  0 tools" in said


def test_a_local_session_stays_local_across_a_profile_switch(monkeypatch):
    """The reported fallback (user, 2026-11-08).

    `/model` switches to a local endpoint by rebuilding the client, and
    deliberately leaves `full_options` as the Claude-shaped snapshot from
    launch — that is what a switch BACK restores. A profile rebuilt from that
    snapshot therefore dropped `ANTHROPIC_BASE_URL` and the local model, and
    the next turn went to Claude with nothing said about it.
    """
    stub = _repl(monkeypatch, profile="full", local=True)
    asyncio.run(stub._profile("analysis"))
    options = _built(stub)
    assert options.env.get("ANTHROPIC_BASE_URL") == LOCAL_ENV["ANTHROPIC_BASE_URL"]
    assert options.env.get("ANTHROPIC_AUTH_TOKEN") == LOCAL_ENV["ANTHROPIC_AUTH_TOKEN"]
    assert options.model == "qwen3-27b", "the local model, not Claude's"


def test_the_claude_shaped_snapshot_is_not_what_gets_rebuilt(monkeypatch):
    """Named directly, because the two option sets look alike at the call site
    and only one of them says where the request goes."""
    stub = _repl(monkeypatch, profile="full", local=True)
    asyncio.run(stub._profile("deck"))
    assert _built(stub).env != stub.session.full_options.env
    assert _built(stub).model != stub.session.full_options.model


def test_a_lean_session_gets_the_lean_append_for_the_NEW_profile(monkeypatch):
    """`session.lean_append` was built at launch and names the profile this
    session has just stopped running."""
    stub = _repl(monkeypatch, profile="full", local=True, lean=True)
    asyncio.run(stub._profile("writing"))
    append = _append_of(stub)
    assert "LAUNCH LEAN APPEND" not in append, "the launch-time append is stale"
    assert "`writing` profile" in append
    # And it is the LEAN one: no project guide in a small model's prompt.
    assert "PROJECT GUIDE TEXT" not in append


def test_a_lean_session_keeps_its_narrowed_host_tools(monkeypatch):
    from scivo_harness.session import LOCAL_HOST_TOOLS

    stub = _repl(monkeypatch, profile="full", local=True, lean=True)
    asyncio.run(stub._profile("writing"))
    assert _built(stub).tools == list(LOCAL_HOST_TOOLS)


def test_a_failed_switch_comes_back_into_the_same_conversation(monkeypatch):
    """The fallback reconnected with the options as they stood, whose
    `resume` is whatever the session was STARTED with — usually nothing. So
    "still on full" meant "on full, in an empty conversation", and nothing
    said so (review 2026-11-10)."""
    stub = _repl(monkeypatch, fail_connect=True)
    assert stub.session.options.resume is None          # a session started fresh
    asyncio.run(stub._profile("video"))
    fallback = stub.clients[-1]
    assert fallback.connected and fallback.options.resume == "sess-1"
    assert fallback.options.continue_conversation is False
    said = ANSI.sub("", "\n".join(stub.said))
    assert "still on full" in said and "the conversation continues" in said


def test_a_failed_switch_with_no_session_to_return_to_says_so(monkeypatch):
    stub = _repl(monkeypatch, fail_connect=True, session_id=None)
    asyncio.run(stub._profile("video"))
    said = ANSI.sub("", "\n".join(stub.said))
    assert "NEW conversation" in said


def test_a_dead_cli_cannot_fail_the_step_that_replaces_it(monkeypatch):
    """One copy of this sequence suppressed a failed disconnect and two did
    not: a switch away from a CLI that had already gone raised on the way out
    and never started the new one."""
    stub = _repl(monkeypatch)

    async def gone():
        raise RuntimeError("Cannot write to terminated process")
    stub.client.disconnect = gone
    asyncio.run(stub._profile("video"))
    assert stub.clients[-1].connected
    assert stub.session.plan.profile == "video"


# ── a local session is shown the kit it has (msja, 2026-11-13) ───────────────

def test_a_local_switch_reports_the_kit_and_what_it_took_away(monkeypatch):
    """`/profile full` said "261/262 scivo tools" to a local session that then
    carried 37, without the video tools it had a moment before."""
    stub = _repl(monkeypatch, profile="video", local=True, lean=True)
    before = stub.session.kit()
    assert "add_video" in before.kept
    asyncio.run(stub._profile("full"))
    after = stub.session.kit()
    text = _text(stub)
    assert f"profile full: {len(after.kept)}/{len(TOOLS)} scivo tools" in text
    assert "the kit for a local model" in text
    assert "no longer loaded" in text and "add_video" in text
    assert stub.session.rails.loaded == set(after.kept)
    dropped = set(_built(stub).disallowed_tools)
    assert toolsets.PREFIX + "add_video" in dropped


def test_the_menu_counts_what_a_local_session_would_carry(monkeypatch):
    stub = _repl(monkeypatch, profile="video", local=True, lean=True)
    rows = dict((name, kept) for name, kept, _ in stub._profile_rows())
    for name in ("full", "video"):
        plan = toolsets.plan(list(TOOLS), profile=name)
        assert rows[name] == len(stub.session.kit(plan).kept)
    cloud = _repl(monkeypatch, profile="video")
    assert dict((n, k) for n, k, _ in cloud._profile_rows())["full"] == len(TOOLS)
