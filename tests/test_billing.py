"""What pays for a session, and what could stop it.

Scivo borrows Claude Code's harness through the Agent SDK so that inference is
paid for by the customer's own claude.ai subscription — that is the reason the
SDK was chosen over calling the API. Three facts now sit under that choice, and
none of them is visible from inside a session:

  · the SDK sets `CLAUDE_CODE_ENTRYPOINT=sdk-py` on the CLI it spawns
    (claude_agent_sdk/_internal/transport/subprocess_cli.py), so a session is
    identifiable as SDK usage rather than as an interactive one;
  · Anthropic began refusing subscription OAuth tokens outside Claude Code at
    the API layer on 2026-01-09, with one message;
  · its service terms gained a clause on 2026-02-19 saying Free/Pro/Max OAuth
    tokens may not be used with third-party tools OR the Agent SDK.

It works today. The point of reporting it is that on the day it stops, the
session says why instead of looking broken (user, 2026-10-25).
"""
from __future__ import annotations

import pytest

from scivo_harness.providers import (
    CREDENTIAL_REFUSAL, SDK_ENTRYPOINT, Provider, billing_lines,
)


@pytest.fixture(autouse=True)
def _no_inherited_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def _state(monkeypatch, state: dict):
    monkeypatch.setattr("scivo_harness.providers.credential_state", lambda: state)


def test_a_subscription_session_is_told_what_could_stop_it(monkeypatch):
    _state(monkeypatch, {"loggedIn": True, "authMethod": "claude.ai",
                         "subscriptionType": "max", "apiProvider": "firstParty"})
    lines = billing_lines(Provider(name="anthropic"))
    joined = " ".join(lines)
    assert "claude.ai login · max" in lines[0]
    assert SDK_ENTRYPOINT in joined                 # how the session identifies
    assert "2026-02-19" in joined and "2026-01-09" in joined
    assert CREDENTIAL_REFUSAL in joined             # the exact text to watch for
    # The recovery is a different HOST, not a different bill: the MCP and the
    # skills run in Claude Code directly, on the same plan.
    assert "Claude Code directly" in joined
    assert "never taken on its own" in joined


def test_an_api_key_needs_no_lecture(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    lines = billing_lines(Provider(name="anthropic"))
    assert lines == ["$ANTHROPIC_API_KEY — metered per token on that account, not a plan"]


def test_a_local_endpoint_says_the_subscription_is_not_involved(monkeypatch):
    lines = billing_lines(Provider(name="qwen", base_url="http://127.0.0.1:8080"))
    assert len(lines) == 1 and "no Anthropic subscription" in lines[0]


def test_a_console_login_is_one_line(monkeypatch):
    """An Anthropic Console org bills its own API account; the clause about
    subscription OAuth does not apply, so saying it would be noise."""
    _state(monkeypatch, {"loggedIn": True, "authMethod": "console",
                         "subscriptionType": None, "apiProvider": "firstParty"})
    assert len(billing_lines(Provider(name="anthropic"))) == 1


def test_not_signed_in_and_unreadable_are_different_answers(monkeypatch):
    _state(monkeypatch, {"loggedIn": False})
    assert billing_lines(Provider(name="anthropic")) == ["not signed in"]
    _state(monkeypatch, {})
    assert billing_lines(Provider(name="anthropic")) == [
        "could not read `claude auth status`"]


def test_the_refusal_text_is_the_one_anthropic_sends():
    """Copied from the error third-party clients received on 2026-01-09; the
    result handler matches on it to explain the failure rather than print it
    raw, so a paraphrase here would silently stop matching."""
    assert CREDENTIAL_REFUSAL == "only authorized for use with Claude Code"
    real = ("This credential is only authorized for use with Claude Code and "
            "cannot be used for other API requests.")
    assert CREDENTIAL_REFUSAL in real
