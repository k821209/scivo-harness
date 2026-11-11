"""Where a session goes is the provider's to say, not a variable left in a shell.

The CLI inherits this process's environment and `options.env` only adds to
it, so an exported ANTHROPIC_BASE_URL sent a session somewhere the harness
never chose while `scivo doctor` said "anthropic → api.anthropic.com".
Checked against the real CLI (2026-11-11): a stray base URL hangs a one-line
prompt in retries, CLAUDE_CODE_USE_BEDROCK=1 fails it, and "" is unset.
"""
from __future__ import annotations

import pytest

from scivo_harness.providers import Provider, ROUTING_VARS, ignored_routing, prepare, routing_note


@pytest.fixture
def stray(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gateway.example:8080")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)


def test_the_built_in_provider_blanks_every_routing_variable(stray):
    env = Provider(name="anthropic").resolve_env()
    for var in ROUTING_VARS:
        assert env[var] == "", var


def test_a_provider_with_its_own_endpoint_keeps_it_and_blanks_the_rest(stray):
    env = Provider(name="local", base_url="http://localhost:8190", auth_token="x").resolve_env()
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8190"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "" and env["CLAUDE_CODE_USE_VERTEX"] == ""


def test_a_provider_that_sets_one_itself_is_obeyed(stray):
    """The provider's own `env` table is applied last: that IS the provider
    saying where to go."""
    env = Provider(name="bedrock", env={"CLAUDE_CODE_USE_BEDROCK": "1"}).resolve_env()
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["ANTHROPIC_BASE_URL"] == ""


def test_api_billing_is_a_decision_to_follow_the_shell(stray):
    env = Provider(name="anthropic").resolve_env(api_billing=True)
    assert not any(var in env for var in ROUTING_VARS)
    assert ignored_routing(Provider(name="anthropic"), api_billing=True) == []


def test_what_is_ignored_is_said_and_names_the_variable(stray):
    note = routing_note(Provider(name="anthropic"))
    assert "$ANTHROPIC_BASE_URL" in note and "$CLAUDE_CODE_USE_BEDROCK" in note
    assert "IGNORED" in note and "providers.toml" in note
    # At session start, where someone who exported it on purpose is looking.
    assert prepare(Provider(name="anthropic")).note == note


def test_nothing_is_said_when_nothing_is_ignored(monkeypatch):
    """The other direction: a line on every start is a line nobody reads."""
    for var in ROUTING_VARS:
        monkeypatch.delenv(var, raising=False)
    assert routing_note(Provider(name="anthropic")) is None
    assert prepare(Provider(name="anthropic")).note is None


def test_a_variable_the_provider_follows_is_not_called_ignored(stray):
    assert ignored_routing(Provider(name="bedrock", env={"CLAUDE_CODE_USE_BEDROCK": "1"})) == [
        "ANTHROPIC_BASE_URL"]
    assert ignored_routing(Provider(name="local", base_url="http://x", auth_token="t")) == [
        "CLAUDE_CODE_USE_BEDROCK"]
