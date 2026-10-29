"""Where the requests go, and how big the model is, are two questions.

They were one property. `is_local` meant `bool(base_url)` and thirteen call
sites read it for both meanings, which was harmless while every endpoint behind
a base_url was a small local llama-server — the lean session (short guide,
pruned tool kit) was built for exactly that model. A frontier model served over
an Anthropic-shaped API arrives through the same field, and would have been
demoted to the lean session because of where it is hosted rather than what it
can do: a 600-token guide and 27 of 272 tools, with nothing saying so.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from scivo_harness import session as session_mod, toolsets
from scivo_harness.providers import Provider, ProviderError, load_all

NAMES = [
    "whoami", "list_papers", "get_manuscript", "add_section", "list_reviews",
    "record_analysis_run", "add_video", "youtube_check", "export_deck_to_pptx",
    "render_slide", "generate_image", "get_user_secret",
]


@dataclass
class FakeOptions:
    """Stands in for ClaudeAgentOptions, which the test stub is not a dataclass."""

    tools: list = field(default_factory=lambda: ["everything"])
    system_prompt: object = "the full guide"
    disallowed_tools: list = field(default_factory=list)


def _session(provider: Provider) -> session_mod.Session:
    options = FakeOptions()
    return session_mod.Session(
        options=options, briefing=None, plan=toolsets.plan(NAMES, "full"),
        rails=None, config=None, provider=provider, model="whatever",
        full_options=options, lean_append="the short guide",
        scivo_tools=tuple(NAMES),
    )


def test_an_endpoint_that_does_not_say_still_gets_the_lean_session():
    """Every providers.toml written before `size` existed points at a small
    local model, so saying nothing has to keep meaning what it meant."""
    qwen = Provider(name="qwen", base_url="http://localhost:8190")
    assert qwen.has_endpoint and qwen.lean_profile
    assert _session(qwen).options_for(qwen).tools == session_mod.LOCAL_HOST_TOOLS


def test_a_large_model_behind_an_endpoint_gets_the_session_claude_gets():
    kimi = Provider(name="k", base_url="https://api.example.org", size="large")
    assert kimi.has_endpoint and not kimi.lean_profile
    carried = _session(kimi).options_for(kimi)
    assert carried.tools == ["everything"]
    assert carried.system_prompt == "the full guide"
    assert carried.disallowed_tools == []


def test_the_anthropic_api_is_neither_an_endpoint_nor_lean():
    builtin = load_all()["anthropic"]
    assert not builtin.has_endpoint and not builtin.lean_profile


def test_size_small_is_the_lean_session_wherever_it_is_hosted():
    far = Provider(name="far", base_url="https://gpu.example.org", size="small")
    assert far.lean_profile
    assert _session(far).options_for(far).tools == session_mod.LOCAL_HOST_TOOLS


def test_a_size_that_is_not_a_size_is_refused_by_name():
    with pytest.raises(ProviderError) as caught:
        Provider(name="typo", base_url="http://x", size="frontier")
    assert "frontier" in str(caught.value) and "small, large" in str(caught.value)


def test_size_is_a_providers_toml_key(tmp_path, monkeypatch):
    config = tmp_path / "providers.toml"
    config.write_text('[svc]\nbase_url = "https://api.example.org"\nsize = "large"\n')
    monkeypatch.setenv("SCIVO_PROVIDERS", str(config))
    assert load_all()["svc"].lean_profile is False


def test_a_gateway_with_a_real_token_is_still_billed_per_token(monkeypatch):
    """`size` must not touch the billing answer: what pays is the token, not
    how big the model is."""
    from scivo_harness.repl import _billed_to_api

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for size in (None, "small", "large"):
        gateway = Provider(name="g", base_url="https://api.example.org",
                           auth_token_env="SCIVO_GATEWAY_TOKEN", size=size)
        assert _billed_to_api(gateway) is True, size
    # And a subscription session is never billed, whatever else changes.
    assert _billed_to_api(Provider(name="anthropic")) is False
