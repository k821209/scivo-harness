"""`/update` has to hand the session back in the state it was in.

Leaving, updating and coming back was four commands typed by hand, so /update
does it — and it already restored the control page, because the page is part
of "this session". The permission mode and the model are too, and neither
lives in argv: `/dangerously-skip-permissions` and `/model` switch them
mid-session, so an update handed the session back in whatever mode it had
STARTED in. Half way through an unattended run, that means it quietly starts
asking again (user, 2026-10-19).
"""
from __future__ import annotations

from scivo_harness.cli import _relaunch_argv


def test_a_mode_switched_mid_session_comes_back():
    argv = _relaunch_argv(["-c"], "sess-1", False,
                          permission_mode="bypassPermissions", model="claude-opus-5")
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--resume") + 1] == "sess-1"


def test_the_live_mode_replaces_the_one_argv_started_with():
    """Started in default, switched to bypass: the switch is what comes back."""
    for started in (["--permission-mode", "default"], ["--permission-mode=default"]):
        argv = _relaunch_argv([*started, "--profile", "video"], "s", False,
                              permission_mode="bypassPermissions")
        assert argv.count("--permission-mode") == 1, argv
        assert "default" not in argv, argv
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
        # An unrelated flag the run was started with survives.
        assert argv[argv.index("--profile") + 1] == "video"


def test_a_mode_switched_back_off_stays_off():
    argv = _relaunch_argv(["--permission-mode", "bypassPermissions"], "s", False,
                          permission_mode="default")
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv.count("bypassPermissions") == 0


def test_the_model_travels_but_only_when_it_was_given():
    with_model = _relaunch_argv(["--model", "claude-sonnet-5"], "s", False,
                                permission_mode="default", model="claude-opus-5")
    assert with_model[with_model.index("--model") + 1] == "claude-opus-5"
    assert "claude-sonnet-5" not in with_model
    # A local provider switch starts a new conversation, so no model is carried.
    local = _relaunch_argv(["--provider", "local"], "s", False,
                           permission_mode="default", model=None)
    assert "--model" not in local
    assert local[local.index("--provider") + 1] == "local"


def test_the_control_page_and_session_choice_still_behave():
    argv = _relaunch_argv(["--control", "-c", "chat"], None, True)
    assert argv.count("--control") == 1
    assert "-c" in argv and "chat" not in argv
    assert "--resume" not in argv


def test_nothing_is_added_when_there_is_nothing_to_carry():
    assert _relaunch_argv(["--profile", "deck"], "s", False) == [
        "--profile", "deck", "--resume", "s"]
