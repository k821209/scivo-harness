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


def test_a_tool_no_domain_claims_is_carried_by_every_profile():
    for profile in toolsets.PROFILES:
        kept = set(toolsets.plan(NAMES, profile).kept)
        assert {"build_tracked_changes", "lint_results_grid", "whoami"} <= kept, profile


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
        assert text.index("chunk-video order") < text.index("STOP")
    # a paper session does not carry it; a lean full session does
    assert "chunk-video order" not in prompt.build(brief, toolsets.plan(NAMES, "paper"), guide="# g")
    assert "chunk-video order" in prompt.build(brief, toolsets.plan(NAMES, "full"), guide=None)
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
