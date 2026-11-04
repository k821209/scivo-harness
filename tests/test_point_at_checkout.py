"""Never point a session at code older than what it was running.

`scivo update` pulls the checkout the session already runs and only then
writes the pointer — so the FIRST update on a project that did not name one
skipped the pull and pinned it to a clone 79 commits behind. The update handed
the session August code, and `whoami` then reported `installed_version
0.1.20260824` with an editable install against `~/co-scientist-mcp-public`
(2026-11-04). The fixes shipped that same day were not running anywhere.

So the pointer is written only after the checkout is current, and a checkout
that cannot be brought current is left alone with a reason.
"""
from __future__ import annotations

import json

import pytest

from scivo_harness import setup as setup_mod


@pytest.fixture()
def project(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"scivo": {"command": "/python", "args": ["-m", "co_scientist_local"],
                                 "env": {"CO_SCIENTIST_API_KEY": "k"}}}}), encoding="utf-8")
    return tmp_path


def _mcp(project):
    return json.loads((project / ".mcp.json").read_text(encoding="utf-8"))


def _checkout(tmp_path, monkeypatch, *, pull_ok: bool, pulls: list):
    checkout = tmp_path / "clone"
    (checkout / "apps" / "local-mcp").mkdir(parents=True)
    monkeypatch.setattr(setup_mod, "usable_checkout", lambda *_a, **_k: (checkout, None))
    from scivo_harness import update as update_mod
    monkeypatch.setattr(update_mod, "pull_checkout",
                        lambda path: (pulls.append(path), (pull_ok, "pulled a → b"))[1])
    return checkout


def test_the_checkout_is_pulled_before_the_pointer_is_written(project, tmp_path, monkeypatch):
    pulls: list = []
    checkout = _checkout(tmp_path, monkeypatch, pull_ok=True, pulls=pulls)
    out = setup_mod.point_at_checkout(project)
    assert pulls == [checkout], "pointed at a checkout without bringing it current"
    assert out and str(checkout) in out
    assert _mcp(project)["mcpServers"]["scivo"]["env"]["PYTHONPATH"] == str(
        checkout / "apps" / "local-mcp")


def test_a_checkout_that_cannot_be_pulled_is_not_pointed_at(project, tmp_path, monkeypatch):
    pulls: list = []
    _checkout(tmp_path, monkeypatch, pull_ok=False, pulls=pulls)
    out = setup_mod.point_at_checkout(project)
    assert pulls, "it did not even try"
    # The project keeps its installed copy, and the reason is said out loud.
    assert "PYTHONPATH" not in _mcp(project)["mcpServers"]["scivo"]["env"]
    assert out and "left on its installed copy" in out


def test_a_project_already_pointed_there_is_left_alone(project, tmp_path, monkeypatch):
    pulls: list = []
    checkout = _checkout(tmp_path, monkeypatch, pull_ok=True, pulls=pulls)
    data = _mcp(project)
    data["mcpServers"]["scivo"]["env"]["PYTHONPATH"] = str(checkout / "apps" / "local-mcp")
    (project / ".mcp.json").write_text(json.dumps(data), encoding="utf-8")
    assert setup_mod.point_at_checkout(project) is None
    # `scivo update` pulls the checkout a session already runs, before this —
    # pulling again here would be the same work twice.
    assert pulls == []


def test_no_checkout_on_the_machine_changes_nothing(project, monkeypatch):
    monkeypatch.setattr(setup_mod, "usable_checkout", lambda *_a, **_k: (None, None))
    assert setup_mod.point_at_checkout(project) is None
    assert "PYTHONPATH" not in _mcp(project)["mcpServers"]["scivo"]["env"]
