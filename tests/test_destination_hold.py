"""A tool named like a read does not get to write anywhere it likes.

`get_asset`, `get_material`, `get_export` and their cousins start with `get_`,
so they are approved without a prompt — and each takes a destination the MODEL
chooses, expands `~`, and overwrites what is there. Write is scoped to the
project; these were not (review 2026-11-10).
"""
from __future__ import annotations

import asyncio

import pytest

from scivo_harness.guardrails import DESTINATION_ARG, Guardrails


def _decision(rails, tool, **args):
    out = asyncio.run(rails.on_destination(
        {"tool_name": f"mcp__scivo__{tool}", "tool_input": args}, None, None))
    spec = out.get("hookSpecificOutput") or {}
    return spec.get("permissionDecision"), spec.get("permissionDecisionReason", "")


@pytest.fixture
def rails(tmp_path):
    (tmp_path / "project").mkdir()
    (tmp_path / "renders").mkdir()
    return Guardrails(root=tmp_path / "project", extra_roots=[str(tmp_path / "renders")])


def test_inside_the_project_is_not_asked_about(rails, tmp_path):
    for args in ({"dest_path": "figures/a.png"}, {"dest_dir": "."}, {"dest_dir": "downloads"},
                 {"dest_path": str(tmp_path / "project" / "x" / "a.png")}, {}):
        assert _decision(rails, "get_material", material_id="m", **args)[0] is None, args


def test_a_directory_the_user_added_is_inside_too(rails, tmp_path):
    assert _decision(rails, "get_asset", asset_id_or_filename="a",
                     dest_path=str(tmp_path / "renders" / "k.png"))[0] is None


@pytest.mark.parametrize("dest", ["~/.bashrc", "/etc/cron.d/x", "../elsewhere/a.png",
                                  "figures/../../escape.png"])
def test_outside_the_project_is_held_for_the_person(rails, dest):
    decision, reason = _decision(rails, "get_asset", asset_id_or_filename="a", dest_path=dest)
    assert decision == "ask"
    assert "OUTSIDE this project" in reason and "overwrites" in reason


def test_it_is_an_ask_not_a_refusal(rails):
    """Saving an export to ~/Downloads is a reasonable thing to want."""
    assert _decision(rails, "get_export", slug="p", filename="x.docx",
                     dest_dir="~/Downloads")[0] == "ask"


def test_a_symlink_out_of_the_project_does_not_count_as_inside(rails, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "project" / "link").symlink_to(outside)
    assert _decision(rails, "get_material", material_id="m",
                     dest_path="link/a.bin")[0] == "ask"


def test_any_tool_with_a_destination_is_covered_not_a_list_of_names(rails):
    """Derived from the argument's shape: a download tool added tomorrow is
    held the day it ships."""
    assert _decision(rails, "get_something_new", output_path="/tmp/x")[0] == "ask"
    assert _decision(rails, "export_to_path", slug="p", output_path="/tmp/x.docx")[0] == "ask"
    for name in ("dest_path", "dest_dir", "out_path", "output_path", "output_dir", "save_path", "dest"):
        assert DESTINATION_ARG.match(name), name
    # …and arguments that READ from a path, or merely name one, are not.
    for name in ("local_path", "log_path", "path", "workdir", "blob_path", "file_path"):
        assert not DESTINATION_ARG.match(name), name


def test_reading_from_outside_is_not_this_rules_business(rails):
    assert _decision(rails, "add_material", local_path="/data/big/file.pdf")[0] is None


def test_the_hosts_own_tools_are_left_to_the_host(rails):
    out = asyncio.run(rails.on_destination(
        {"tool_name": "Write", "tool_input": {"file_path": "/etc/x", "dest_path": "/etc/x"}}, None, None))
    assert out == {}


def test_without_a_project_root_nothing_is_guessed():
    assert Guardrails().outside_project({"dest_path": "/etc/x"}) == []


def test_it_is_recorded_for_blocked(rails):
    _decision(rails, "get_asset", asset_id_or_filename="a", dest_path="/tmp/x")
    assert any("outside the project" in line for line in rails.blocked)


def test_the_hook_is_registered_for_every_tool(rails):
    from scivo_harness import guardrails as G
    seen = []
    original = G.HookMatcher
    G.HookMatcher = lambda matcher=None, hooks=None: seen.append((matcher, hooks)) or (matcher, hooks)
    try:
        G.build(rails)
    finally:
        G.HookMatcher = original
    assert any(matcher is None and rails.on_destination in (hooks or []) for matcher, hooks in seen)
