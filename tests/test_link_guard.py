"""`scivo update` re-links the skills; it now wires the ssh guard for plain
Claude Code in the same step, and says what happened either way."""
from __future__ import annotations

import types

from scivo_harness import update


def _config():
    return types.SimpleNamespace(command="/py", root="/proj", child_env=lambda: {})


def _fake_run(monkeypatch, answers):
    calls = []

    def run(argv, **_kw):
        calls.append(argv)
        return answers[argv[3]]
    monkeypatch.setattr(update, "_run", run)
    return calls


def test_the_guard_is_wired_when_the_skills_are(monkeypatch):
    calls = _fake_run(monkeypatch, {"install-skills": (0, "linked 32 skills"),
                                    "install-hooks": (0, "✓ Wrote …")})
    ok, out = update.link_skills(_config())
    assert ok and "ssh provenance guard installed for Claude Code" in out
    assert calls[1][3:7] == ["install-hooks", "--host", "claude", "--dir"]


def test_an_older_mcp_is_named_and_does_not_fail_the_update(monkeypatch):
    _fake_run(monkeypatch, {
        "install-skills": (0, "linked"),
        "install-hooks": (2, "error: argument --host: invalid choice: 'claude' (choose from 'codex')")})
    ok, out = update.link_skills(_config())
    assert ok
    assert "not installed: this MCP is older than the installer" in out


def test_any_other_failure_is_reported_with_its_reason(monkeypatch):
    _fake_run(monkeypatch, {
        "install-skills": (0, "linked"),
        "install-hooks": (1, "✗ /proj/.claude/settings.local.json is not valid JSON; left untouched")})
    ok, out = update.link_skills(_config())
    assert ok and "not valid JSON" in out


def test_the_guard_is_not_attempted_when_the_skills_failed(monkeypatch):
    calls = _fake_run(monkeypatch, {"install-skills": (1, "boom")})
    ok, _out = update.link_skills(_config())
    assert not ok and len(calls) == 1


def test_a_session_marks_itself_so_the_file_guard_stands_down():
    import inspect
    from scivo_harness import session
    assert session.session_env()["SCIVO_HARNESS"] == "1"
    assert "**session_env()" in inspect.getsource(session.build)


def test_the_mark_survives_a_switch_to_another_endpoint():
    """`/model <local>` rebuilt the environment from the endpoint's alone. The
    mark was gone, the hook file woke up, and one msja turn was refused 771
    times by the file and 358 times in process — two layers, two sets of
    rules, and an override only one of them knew (2026-11-12). The test above
    passed throughout: it read the string in `build`."""
    import types
    from dataclasses import dataclass, field

    from scivo_harness import session

    @dataclass
    class Opts:
        env: dict = field(default_factory=dict)
        model: str = "claude-opus-5"

    stub = types.SimpleNamespace(full_options=Opts(env={"SCIVO_HARNESS": "1"}))
    cloud = types.SimpleNamespace(lean_profile=False)
    endpoint = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8099", "MCP_DISCOVERY_CACHE": "true"}
    built = session.Session.options_for(stub, cloud, env=endpoint, model="qwen")
    assert built.env["SCIVO_HARNESS"] == "1"
    assert built.env["SCIVO_CLIENT_ID"] == session.session_client_id()
    assert built.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8099"
    assert built.env["MCP_DISCOVERY_CACHE"] == "true"      # an endpoint may still override
