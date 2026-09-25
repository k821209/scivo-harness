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
