"""The probe is the gate, so it has to talk the way a session will.

Both probes sent `auth_token or "unused"` as `x-api-key`. That is exactly right
for a local llama-server, which ignores the value, and useless for a gateway
that does not: a provider keeping its token in the environment — the only place
a real token is allowed to live — was probed with the literal string "unused",
so `scivo doctor` could only ever report 401 for it. A gate that cannot pass is
not a gate.
"""
from __future__ import annotations

import urllib.error

from scivo_harness.providers import Provider, probe, probe_headers, probe_token


def test_a_local_server_is_probed_the_way_the_session_will_talk_to_it():
    local = Provider(name="tiny", base_url="http://localhost:8190", auth_token="unused")
    headers = probe_headers(local)
    # resolve_env hands the CLI ANTHROPIC_AUTH_TOKEN="unused" and the CLI sends
    # that as a bearer, so the probe sends the same thing, placeholder and all.
    assert headers["Authorization"] == "Bearer unused"
    assert headers["x-api-key"] == "unused"


def test_a_provider_with_no_token_at_all_invents_none():
    headers = probe_headers(Provider(name="bare", base_url="http://localhost:8190"))
    assert headers["x-api-key"] == "unused"
    assert "Authorization" not in headers


def test_a_token_from_the_environment_reaches_the_probe(monkeypatch):
    monkeypatch.setenv("SCIVO_SVC_TOKEN", "sk-real-token")
    svc = Provider(name="svc", base_url="https://api.example.org",
                   auth_token_env="SCIVO_SVC_TOKEN")
    assert probe_token(svc) == "sk-real-token"
    headers = probe_headers(svc)
    # The CLI is handed ANTHROPIC_AUTH_TOKEN, which it sends as a bearer; the
    # x-api-key copy is for a local server started with --api-key.
    assert headers["Authorization"] == "Bearer sk-real-token"
    assert headers["x-api-key"] == "sk-real-token"


def test_a_missing_token_is_said_before_anything_is_sent(monkeypatch):
    monkeypatch.delenv("SCIVO_SVC_TOKEN", raising=False)

    def never(*args, **kwargs):
        raise AssertionError("the probe asked the network with no token")

    monkeypatch.setattr("urllib.request.urlopen", never)
    svc = Provider(name="svc", base_url="https://api.example.org",
                   auth_token_env="SCIVO_SVC_TOKEN")
    ok, detail = probe(svc)
    assert ok is False
    assert "$SCIVO_SVC_TOKEN" in detail and "not set" in detail


def test_a_refused_token_is_not_reported_as_a_broken_route(monkeypatch):
    """401 used to come back as a bare "HTTP 401 on /v1/messages", which reads
    as "the endpoint is wrong" when the endpoint is fine and the key is not."""
    monkeypatch.setenv("SCIVO_SVC_TOKEN", "sk-wrong")

    def refuse(*args, **kwargs):
        raise urllib.error.HTTPError("https://api.example.org/v1/messages", 401,
                                     "Unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    svc = Provider(name="svc", base_url="https://api.example.org",
                   auth_token_env="SCIVO_SVC_TOKEN")
    ok, detail = probe(svc)
    assert ok is False
    assert "401" in detail and "refused the token" in detail
    assert "$SCIVO_SVC_TOKEN" in detail


def test_a_404_still_names_the_one_cause_that_explains_it(monkeypatch):
    def not_found(*args, **kwargs):
        raise urllib.error.HTTPError("http://localhost:8190/v1/messages", 404,
                                     "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", not_found)
    ok, detail = probe(Provider(name="tiny", base_url="http://localhost:8190"))
    assert ok is False and "chat-completions" in detail
