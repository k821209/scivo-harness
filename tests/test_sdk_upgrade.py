"""`scivo update` moves claude-agent-sdk, because the CLI ships inside it.

Nothing was moving it before. `apply()` reinstalls with `--no-deps` to protect
pins other projects share, and `fill_missing_deps` installs only what is
MISSING — so an SDK already present stayed exactly where it was while
`claude update` moved a binary no session ever spawns. One machine sat at
bundled 2.1.274 against 2.1.319 on PATH, 45 versions apart, until the API
refused a model and told the user to run the command that could not help
(user, 2026-11-06).
"""
from __future__ import annotations

import json

import pytest

from scivo_harness import update


def _fake_run(states, pip_rc=0, pip_out="Successfully installed"):
    """_run stand-in: pops a probe answer per probe call, records pip argv."""
    calls: list[list[str]] = []
    answers = list(states)

    def run(argv, cwd=None, *, timeout=120, env=None):
        calls.append(argv)
        if "-c" in argv:
            nxt = answers.pop(0) if answers else None
            if nxt is None:
                return 1, ""                      # probe could not be read
            return 0, json.dumps(nxt)
        return pip_rc, pip_out
    run.calls = calls
    return run


def test_it_reports_what_moved_including_the_cli_version(monkeypatch):
    monkeypatch.setattr(update, "_run", _fake_run([
        {"sdk": "0.2.100", "cli": "2.1.274", "bundled": True},
        {"sdk": "0.2.176", "cli": "2.1.319", "bundled": True},
    ]))
    state, detail = update.upgrade_sdk("/x/python")
    assert state == "moved"
    # Both numbers, because "updated" alone does not say whether the thing
    # that mattered — the CLI — actually changed.
    assert "2.1.274" in detail and "2.1.319" in detail
    assert "0.2.100" in detail and "0.2.176" in detail


def test_a_cli_that_moved_under_the_same_sdk_version_still_counts(monkeypatch):
    """The CLI is the thing being tracked, not the package number.

    A wheel rebuilt at the same version carries a different binary, and the
    binary is what decides which models a session can drive. Keying "moved"
    off the sdk version alone reports "already the latest" while the CLI the
    user is waiting for has in fact just changed — and the restart notice,
    which only prints on `moved`, would not fire.
    """
    monkeypatch.setattr(update, "_run", _fake_run([
        {"sdk": "0.2.176", "cli": "2.1.274", "bundled": True},
        {"sdk": "0.2.176", "cli": "2.1.319", "bundled": True},
    ]))
    state, detail = update.upgrade_sdk("/x/python")
    assert state == "moved"
    assert "2.1.274" in detail and "2.1.319" in detail


def test_the_pip_call_keeps_no_deps(monkeypatch):
    run = _fake_run([
        {"sdk": "0.2.176", "cli": "2.1.316", "bundled": True},
        {"sdk": "0.2.176", "cli": "2.1.316", "bundled": True},
    ])
    monkeypatch.setattr(update, "_run", run)
    update.upgrade_sdk("/x/python")
    pip = next(a for a in run.calls if "pip" in a)
    assert pip[:2] == ["/x/python", "-m"]
    assert "--no-deps" in pip and "claude-agent-sdk" in pip
    # Not --force-reinstall: this one is allowed to be a no-op when current.
    assert "--force-reinstall" not in pip


def test_already_current_says_so_with_the_version_it_checked(monkeypatch):
    monkeypatch.setattr(update, "_run", _fake_run([
        {"sdk": "0.2.176", "cli": "2.1.316", "bundled": True},
        {"sdk": "0.2.176", "cli": "2.1.316", "bundled": True},
    ]))
    state, detail = update.upgrade_sdk("/x/python")
    assert state == "current"
    assert "2.1.316" in detail and "already the latest" in detail


def test_an_sdk_that_carries_no_cli_says_path_is_in_use(monkeypatch):
    monkeypatch.setattr(update, "_run", _fake_run([
        {"sdk": "0.2.176", "cli": None, "bundled": False},
        {"sdk": "0.2.176", "cli": None, "bundled": False},
    ]))
    state, detail = update.upgrade_sdk("/x/python")
    assert state == "current"
    assert "no CLI" in detail and "PATH" in detail


def test_a_missing_sdk_is_absent_not_failed(monkeypatch):
    monkeypatch.setattr(update, "_run", _fake_run([
        {"sdk": None, "cli": None, "bundled": False},
    ]))
    state, detail = update.upgrade_sdk("/x/python")
    assert state == "absent" and "not installed" in detail


def test_a_failed_pip_is_failed_and_carries_its_words(monkeypatch):
    monkeypatch.setattr(update, "_run", _fake_run(
        [{"sdk": "0.2.100", "cli": "2.1.274", "bundled": True}],
        pip_rc=1, pip_out="ERROR: No matching distribution found"))
    state, detail = update.upgrade_sdk("/x/python")
    assert state == "failed" and "No matching distribution" in detail


@pytest.mark.parametrize("probes, why", [
    ([None], "before"),
    ([{"sdk": "0.2.100", "cli": "2.1.274", "bundled": True}, None], "after"),
])
def test_an_unreadable_probe_is_unknown_never_current(monkeypatch, probes, why):
    """The state that must exist: pip said nothing wrong, and we still cannot say.

    Collapsing this into "current" would print a pass for a check that never
    ran — and after the upgrade it is the WORSE direction, because the user
    would be told everything is fine while still holding the old CLI.
    """
    monkeypatch.setattr(update, "_run", _fake_run(probes))
    state, detail = update.upgrade_sdk("/x/python")
    assert state == "unknown", why
    assert state != "current"
    assert "could not" in detail
