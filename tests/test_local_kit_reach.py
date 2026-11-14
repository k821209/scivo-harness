"""Every tool of the server is reachable from a local-model session, or the
session says it is not.

A local model carries a cut-down kit. On 2026-11-15 an msja session on a local
model typed `/profile full` to get `list_pipelines`, was told "40/262 scivo
tools", and had no way to load the pipeline tools at all: they sat in the
`analysis` domain, which had grown to 62 tools, two over what a local kit takes
— so `/profile analysis`, which the banner, `/tools` and the prompt all named as
the way to get them, loaded nothing either. The model spent 26 turns reading
the MCP source to find out why.

The names are the server's own, listed on that day (MCP v0.1.20261114.post2,
video on) — a hand-picked list is how a domain two tools over the limit went
unnoticed.
"""
from __future__ import annotations

import pathlib

import pytest

from scivo_harness import session, toolsets

REAL = tuple((pathlib.Path(__file__).parent / "data" / "scivo_tools_2026-11-15.txt")
             .read_text().split())
PIPELINE = {name for name in REAL if "pipeline" in name}


def local(profile: str) -> toolsets.ToolPlan:
    return session.kit_plan(toolsets.plan(list(REAL), profile), REAL, True)


def test_the_list_is_the_servers():
    assert len(REAL) == 262 and len(PIPELINE) == 11
    assert {"list_pipelines", "get_pipeline", "register_pipeline_version",
            "search_public_pipelines"} <= PIPELINE


def test_a_local_session_can_load_the_pipeline_tools():
    assert PIPELINE <= set(local("pipeline").kept)
    assert not PIPELINE & set(local("full").kept), "full is still the core alone"


def test_a_cloud_session_lost_nothing_by_the_split():
    for profile in ("full", "paper", "analysis"):
        assert PIPELINE <= set(toolsets.plan(list(REAL), profile).kept), profile
    assert not PIPELINE & set(toolsets.plan(list(REAL), "video").kept)
    # One tool fewer than the server has: `wait_for_responses` is the harness's own.
    assert len(toolsets.plan(list(REAL), "full").kept) == len(REAL) - len(toolsets.HARNESS_ONLY)


def test_profiles_combine_so_getting_one_domain_does_not_cost_another():
    """The same session had been sent from `video` to `full` two days earlier
    and lost the video tools. A profile is added, not swapped."""
    both = local("video+pipeline")
    assert PIPELINE <= set(both.kept) and "add_video" in both.kept
    assert set(both.kept) == set(local("video").kept) | set(local("pipeline").kept)
    assert both.profile == "video+pipeline"
    assert toolsets.canonical("pipeline+video") == "video+pipeline", "one spelling"
    assert toolsets.canonical("video+video") == "video"
    cloud = toolsets.plan(list(REAL), "video+pipeline")
    assert set(cloud.kept) == (set(toolsets.plan(list(REAL), "video").kept)
                               | set(toolsets.plan(list(REAL), "pipeline").kept))


def test_full_plus_a_profile_is_still_full_on_a_cloud_model():
    """`+pipeline` typed in a default local session makes `full+pipeline`;
    switching that session to a Claude model must not narrow it to pipelines."""
    assert (set(toolsets.plan(list(REAL), "full+pipeline").kept)
            == set(toolsets.plan(list(REAL), "full").kept))
    assert set(local("full+pipeline").kept) == set(local("full").kept) | PIPELINE


def test_a_combination_with_none_or_an_unknown_name_is_refused_by_name():
    with pytest.raises(ValueError, match="none"):
        toolsets.canonical("none+video")
    with pytest.raises(ValueError, match="pipelines"):
        toolsets.canonical("video+pipelines")
    with pytest.raises(ValueError):
        toolsets.plan(list(REAL), "video+nope")


def test_every_profile_a_local_session_is_offered_adds_something():
    """The banner said "`--profile video | deck | analysis` loads that domain"
    while `analysis` loaded nothing. What is offered is what was measured."""
    kit = local("full")
    offered = dict(kit.offers)
    assert offered, "a local full session is offered nothing"
    core = set(kit.kept)
    for profile, names in offered.items():
        added = set(local(f"full+{profile}").kept) - core
        assert added and set(names) <= added, profile
    assert "pipeline" in offered and "list_pipelines" in offered["pipeline"]
    assert "analysis" in offered and "video" in offered


def test_each_absent_tool_is_filed_once_under_the_smallest_profile_that_loads_it():
    kit = local("video")
    filed = [name for _, names in kit.offers for name in names]
    assert len(filed) == len(set(filed))
    assert not set(filed) & set(kit.kept)
    assert set(filed) | set(kit.kept) | set(kit.unreachable) == set(REAL) - set(toolsets.HARNESS_ONLY)
    where = {name: profile for profile, names in kit.offers for name in names}
    assert where["list_pipelines"] == "pipeline", "not `analysis`, which carries 50 more"
    assert where["add_slide"] == "deck"


def test_a_domain_too_large_for_a_local_kit_is_named_not_dropped_in_silence():
    """`paper` is 114 tools. A session asked for it and got the materials
    tools, with a count and no word about the rest."""
    kit = local("paper")
    assert [domain for domain, _ in kit.left_out] == ["paper"]
    assert dict(kit.left_out)["paper"] > session.LOCAL_DOMAIN_LIMIT
    assert "paper" in kit.summary() and "too large" in kit.summary()
    assert kit.unreachable and "update_table" in kit.unreachable
    assert local("video").left_out == ()
    assert "too large" not in local("video").summary()


def test_the_analysis_profile_loads_on_a_local_model_again():
    kit = local("analysis")
    assert {"list_datasets", "launch_local_job", "list_pipelines"} <= set(kit.kept)
    assert kit.left_out == ()


def test_no_asked_for_domain_is_ever_skipped_without_being_reported(monkeypatch):
    """The next tool added to a domain at the limit: it must show."""
    monkeypatch.setattr(session, "LOCAL_DOMAIN_LIMIT", 5)
    kit = local("video")
    assert {domain for domain, _ in kit.left_out} == {"video", "materials"}
    assert "add_video" not in kit.kept and "add_video" in kit.unreachable
    assert "video" in kit.summary()


def test_the_local_prompt_says_where_each_absent_tool_is():
    from scivo_harness import prompt
    from scivo_harness.preflight import Briefing
    said = prompt.build(Briefing(), local("video"), guide=None)
    assert "`/profile +pipeline`" in said and "list_pipelines" in said
    assert "`/profile +video`" not in said, "already loaded"
    assert "`/profile +analysis`" in said
    assert "(or `deck`, `analysis`)" not in said, "the sentence that was not true"
    # What no profile reaches is described, not listed: 120 names nobody can
    # load from here are 120 names carried on every request.
    note = prompt._tool_note(local("full"))
    assert "a Claude model only" in note and "120 more" in note
    assert "update_table" not in note and "table" in note.split("a Claude model only")[1]
    assert len(note) < 4000, len(note)
    # And a domain that was asked for and did not fit is said in words.
    paper = prompt._tool_note(local("paper"))
    assert "`paper` was asked for and is NOT loaded" in paper and "114" in paper
    assert "was asked for" not in note


def test_the_video_order_survives_a_combination():
    from scivo_harness import prompt
    from scivo_harness.preflight import Briefing
    alone = prompt._order_note(local("video"), None)
    assert alone and prompt._order_note(local("video+pipeline"), None) == alone
    assert prompt._order_note(local("pipeline"), None) == ""
    assert alone in prompt.build(Briefing(), local("video+pipeline"), guide=None)


def test_a_read_only_local_session_is_not_offered_tools_that_write():
    """What a profile would add is worked out through the plan, which is what
    takes the writers out — an offer of `register_pipeline` to a read-only
    session is a command that would not deliver it."""
    kit = session.kit_plan(toolsets.plan(list(REAL), "video", read_only=True), REAL, True)
    offered = {name for _, names in kit.offers for name in names}
    assert "list_pipelines" in offered
    assert "register_pipeline" not in offered and "launch_local_job" not in offered
    assert all(toolsets.is_read_only(name) for name in offered)


def test_an_empty_profile_is_offered_nothing_by_addition():
    kit = session.kit_plan(toolsets.plan(list(REAL), "none"), REAL, True)
    assert kit.kept == [] and kit.offers == () and kit.unreachable == ()
