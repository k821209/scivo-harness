"""Tool profiles: what a session carries, and what it never auto-approves."""
from __future__ import annotations

from scivo_harness import toolsets

NAMES = [
    "whoami", "project_guide", "preview_slide", "export_deck_to_pptx", "render_slide",
    "update_slide", "list_papers", "add_section", "list_reviews", "export_to_path",
    "preview_study", "write_study", "publish_page", "put_page_data", "clear_page_data",
    "list_analyses", "record_analysis_run", "compare_run_params", "refresh_log_tail",
    "add_material", "list_assets", "add_video", "youtube_check", "get_user_secret",
    "list_user_secrets", "build_tracked_changes", "lint_results_grid",
    "generate_image", "add_video_chunk", "update_video_chunk", "list_video_chunks",
]


def test_deck_profile_keeps_the_deck_loop_tools():
    kept = set(toolsets.plan(NAMES, "deck").kept)
    assert {"preview_slide", "export_deck_to_pptx", "render_slide", "update_slide"} <= kept
    assert "add_section" not in kept and "list_reviews" not in kept


def test_paper_profile_keeps_paper_and_study_and_page_tools():
    kept = set(toolsets.plan(NAMES, "paper").kept)
    assert {"add_section", "list_reviews", "export_to_path", "preview_study",
            "publish_page", "put_page_data", "clear_page_data"} <= kept
    assert "add_video" not in kept and "youtube_check" not in kept


def test_a_tool_no_domain_claims_is_carried_by_every_profile_that_carries_any():
    # `none` is the one exception and it is the point of it, so the invariant
    # says which profiles it is about rather than being quietly weakened.
    for profile, domains in toolsets.PROFILES.items():
        if not domains:
            continue
        kept = set(toolsets.plan(NAMES, profile).kept)
        assert {"build_tracked_changes", "lint_results_grid", "whoami"} <= kept, profile


def test_the_none_profile_carries_nothing_at_all():
    """"완전 프리한걸로" (user, 2026-11-08) — including the core.

    Going through the ordinary path would have kept `ALWAYS` and every tool
    whose domain falls through to `core`: whoami, memory, todos, decisions,
    feedback. Around forty tools, which is not what "no tools" means.
    """
    plan = toolsets.plan(NAMES, "none")
    assert plan.kept == []
    assert set(plan.dropped) == set(NAMES)
    assert plan.auto_approved == []
    assert "whoami" not in plan.kept, "ALWAYS does not survive it"
    assert all(name.startswith("mcp__scivo__") for name in plan.disallowed)


def test_an_empty_profile_is_the_rule_not_the_name():
    """A second empty profile must behave the same, not silently differently."""
    try:
        toolsets.PROFILES["blank-for-test"] = ()
        assert toolsets.plan(NAMES, "blank-for-test").kept == []
    finally:
        del toolsets.PROFILES["blank-for-test"]


def test_a_read_only_none_session_is_still_nothing():
    plan = toolsets.plan(NAMES, "none", read_only=True)
    assert plan.kept == [] and plan.read_only_session is True


def test_secrets_are_never_auto_approved():
    plan = toolsets.plan(NAMES, "full")
    assert "get_user_secret" in plan.kept
    assert "mcp__scivo__get_user_secret" not in plan.auto_approved
    assert "mcp__scivo__list_user_secrets" not in plan.auto_approved
    assert "mcp__scivo__list_papers" in plan.auto_approved


def test_video_profile_carries_the_chunk_loop_and_the_keyframe_generator():
    kept = set(toolsets.plan(NAMES, "video").kept)
    assert {"add_video_chunk", "update_video_chunk", "list_video_chunks", "add_material",
            "generate_image"} <= kept
    assert "add_section" not in kept
    # generate_image is filed under paper; deck and video borrow it, analysis does not
    assert "generate_image" in toolsets.plan(NAMES, "deck").kept
    assert "generate_image" not in toolsets.plan(NAMES, "analysis").kept


def test_the_video_order_rides_in_the_prompt_with_or_without_the_guide():
    """A local model gets no guide, and the one that had it skimmed the
    paragraph: the chunk order is a numbered list in the prompt itself."""
    from scivo_harness import prompt, session
    from scivo_harness.preflight import Briefing
    plan = toolsets.plan(NAMES, "video")
    brief = Briefing()
    with_guide = prompt.build(brief, plan, guide="# guide")
    without = prompt.build(brief, plan, guide=None)
    for text in (with_guide, without):
        assert "STOP" in text and "generate ONLY the rows" in text
        assert "STRAIGHT to the local" in text   # generate_image down → local model, no detour
        assert 'get_video_chunk_image(video_id, n, which="start"' in text   # the confirmed file, not a local name
        assert text.index("chunk-video order") < text.index("STOP")
    # a paper session does not carry it; a lean full session does
    assert "chunk-video order" not in prompt.build(brief, toolsets.plan(NAMES, "paper"), guide="# g")
    # a full session does not pay for the video order, guide or no guide
    assert "chunk-video order" not in prompt.build(brief, toolsets.plan(NAMES, "full"), guide=None)
    assert "chunk-video order" not in prompt.build(brief, toolsets.plan(NAMES, "full"), guide="# g")
    # and the lean video kit has the keyframe generator
    kit = session.local_kit(tuple(NAMES), "video")
    assert {"generate_image", "add_video_chunk", "update_video_chunk"} <= kit


def test_a_local_model_gets_the_short_guide_and_a_claude_session_the_full_one():
    from scivo_harness import prompt
    from scivo_harness.preflight import Briefing
    plan = toolsets.plan(NAMES, "paper")
    lean = prompt.build(Briefing(), plan, guide=None)
    full = prompt.build(Briefing(), plan, guide="# THE GUIDE")
    assert "short form" in lean and "add_reference_by_doi" in lean and "THE GUIDE" not in lean
    assert "empty result is an answer" in lean
    assert "THE GUIDE" in full and "short form" not in full
    assert len(prompt.LOCAL_GUIDE) < 3000   # ~600 tokens: the point of it


def test_the_cache_note_reads_per_model_figures_then_the_flat_usage():
    from types import SimpleNamespace
    from scivo_harness.repl import cache_note
    per_model = SimpleNamespace(model_usage={"m": {"cacheReadInputTokens": 24500, "cacheCreationInputTokens": 0}}, usage=None)
    assert cache_note(per_model) == "cache 24.5k read · 0 new"
    flat = SimpleNamespace(model_usage=None, usage={"cache_read_input_tokens": 0, "cache_creation_input_tokens": 1200})
    assert cache_note(flat) == "cache 0 read · 1.2k new"
    assert cache_note(SimpleNamespace(model_usage=None, usage=None)) == ""


def test_a_toolless_session_gets_neither_guide():
    """Both guides are instructions for calling tools, so neither belongs.

    The full guide is dropped when a project guide was fetched; LOCAL_GUIDE is
    the one a lean session gets INSTEAD, and that path has its own branch — a
    small-model session switching to `none` would otherwise be handed a page of
    tool conventions for tools it does not have.
    """
    from scivo_harness import prompt
    from scivo_harness.preflight import Briefing

    empty = toolsets.plan(NAMES, "none")
    assert prompt.LOCAL_GUIDE not in prompt.build(Briefing(), empty, guide=None)
    assert "PROJECT GUIDE" not in prompt.build(Briefing(), empty,
                                               guide="PROJECT GUIDE TEXT")
    # And a profile that DOES carry tools still gets one.
    narrow = toolsets.plan(NAMES, "writing")
    assert prompt.LOCAL_GUIDE in prompt.build(Briefing(), narrow, guide=None)
    assert "PROJECT GUIDE TEXT" in prompt.build(Briefing(), narrow,
                                                guide="PROJECT GUIDE TEXT")


# ── what a refusal sends the model to is always there ────────────────────────

REAL = list(dict.fromkeys(
    NAMES + list(toolsets.PRESCRIBED) + ["list_videos", "list_materials", "get_material"]))


def test_every_profile_that_loads_anything_loads_what_the_guards_prescribe():
    """`--profile video` had no `submit_remote_job`, and the ssh guard named
    it 358 times in one turn (feedback d291849a7b1a)."""
    from scivo_harness import session
    for profile, domains in toolsets.PROFILES.items():
        if not domains:
            continue
        plan = toolsets.plan(REAL, profile)
        assert set(toolsets.PRESCRIBED) <= set(plan.kept), profile
        local = session.kit_plan(plan, tuple(REAL), True)
        assert set(toolsets.PRESCRIBED) <= set(local.kept), f"{profile}, local model"


def test_what_the_guards_name_is_in_the_prescribed_list():
    """Derived from the guard's own text: a refusal written tomorrow that
    names another tool fails here until that tool is carried everywhere."""
    import inspect
    import re
    from scivo_harness import guardrails
    source = inspect.getsource(guardrails)
    named = set(re.findall(r"mcp__scivo__([a-z_]+)", source))
    named |= {name for name in re.findall(r"`([a-z]+_[a-z_]+)`", source)
              if name.startswith(("submit_", "wait_", "kill_", "create_", "record_",
                                  "add_server", "update_server"))}
    assert named, "the scan found nothing — it is not reading the refusals"
    assert named <= set(toolsets.PRESCRIBED) | set(toolsets.ALWAYS), \
        named - set(toolsets.PRESCRIBED) - set(toolsets.ALWAYS)


def test_a_read_only_session_still_drops_the_writers_among_them():
    kept = set(toolsets.plan(REAL, "video", read_only=True).kept)
    assert "submit_remote_job" not in kept and "list_servers" in kept


def test_the_kit_plan_is_what_a_local_session_is_actually_given():
    """One computation: what is reported and what the CLI is told to drop."""
    from dataclasses import dataclass, field
    from scivo_harness import session

    @dataclass
    class Opts:
        disallowed_tools: list = field(default_factory=list)
        tools: list | None = None
        system_prompt: dict = field(default_factory=dict)

    for profile in ("full", "video", "paper", "writing"):
        plan = toolsets.plan(REAL, profile)
        kit = session.kit_plan(plan, tuple(REAL), True)
        given = session.lean(Opts(disallowed_tools=plan.disallowed), "x", tuple(REAL), profile)
        loaded = {name for name in REAL if toolsets.PREFIX + name not in set(given.disallowed_tools)}
        assert loaded == set(kit.kept), profile
        assert kit.lean and "local model" in kit.summary()
        assert len(kit.kept) + len(kit.dropped) == len(REAL)


def test_full_is_not_the_largest_kit_for_a_local_model():
    """The fact the banner hid: `/profile full`, typed to get more, drops the
    video tools a local `video` session had."""
    from scivo_harness import session
    full = session.kit_plan(toolsets.plan(REAL, "full"), tuple(REAL), True)
    video = session.kit_plan(toolsets.plan(REAL, "video"), tuple(REAL), True)
    assert "add_video" in video.kept and "add_video" not in full.kept
    assert len(toolsets.plan(REAL, "full").kept) > len(full.kept)
    assert session.kit_plan(toolsets.plan(REAL, "full"), tuple(REAL), False).lean is False


def test_a_local_session_is_not_told_that_full_carries_everything():
    from scivo_harness import prompt, session
    from scivo_harness.preflight import Briefing
    kit = session.kit_plan(toolsets.plan(REAL, "video"), tuple(REAL), True)
    said = prompt.build(Briefing(), kit, guide=None)
    assert f"{len(kit.kept)} of {len(REAL)} scivo tools are loaded" in said
    assert "`full` here is the core alone" in said
    assert "restart under" not in said
    cloud = prompt.build(Briefing(), toolsets.plan(REAL, "video"), guide=None)
    assert "`scivo --profile full`" in cloud
