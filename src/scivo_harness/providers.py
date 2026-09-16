"""Which model endpoint the session talks to.

The Agent SDK drives the `claude` CLI, and the CLI takes its endpoint from the
environment. `ClaudeAgentOptions.env` is injected into that subprocess, so a
provider here is just a named bundle of environment variables plus a default
model — no patching, no fork.

**The endpoint must speak Anthropic `/v1/messages`, including `tool_use`.**
Recent llama.cpp `llama-server` builds serve that route natively — verified here
against Qwen3.8-27B, which returned a well-formed `tool_use` block — so no proxy
is needed for it. A server that only offers OpenAI chat-completions (older
llama.cpp, plain Ollama) needs a translator in front, such as the LiteLLM proxy;
`scivo doctor` probes the route and says which case you are in.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BUILTIN_NAME = "anthropic"

SEARCH_PATHS = (
    lambda: Path.cwd() / ".scivo" / "providers.toml",
    lambda: Path.home() / ".config" / "scivo" / "providers.toml",
)


class ProviderError(RuntimeError):
    pass


@dataclass
class Provider:
    name: str
    base_url: str | None = None
    model: str | None = None
    small_model: str | None = None
    auth_token: str | None = None
    auth_token_env: str | None = None
    supports_effort: bool = True
    max_output_tokens: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    note: str = ""
    source: str = "built-in"

    @property
    def is_local(self) -> bool:
        return bool(self.base_url)

    def resolve_env(self) -> dict[str, str]:
        """The environment the CLI subprocess runs under."""
        out: dict[str, str] = {}
        if self.base_url:
            out["ANTHROPIC_BASE_URL"] = self.base_url
        token = self.auth_token
        if not token and self.auth_token_env:
            token = os.environ.get(self.auth_token_env)
            if not token:
                raise ProviderError(
                    f"provider '{self.name}' reads its token from ${self.auth_token_env}, "
                    "which is not set."
                )
        if token:
            # A local shim usually ignores the value but the CLI requires one,
            # and an empty string outranks a stored profile without replacing it.
            out["ANTHROPIC_AUTH_TOKEN"] = token
        if self.small_model:
            out["ANTHROPIC_SMALL_FAST_MODEL"] = self.small_model
        if self.max_output_tokens:
            out["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self.max_output_tokens)
        out.update(self.env)
        return out


def _config_path() -> Path | None:
    # An explicit override that silently falls back is a trap: a typo in
    # $SCIVO_PROVIDERS would load a different project's endpoints with nothing
    # to read. If it is set, it is the answer or it is an error.
    override = os.environ.get("SCIVO_PROVIDERS")
    if override:
        path = Path(override)
        if not path.is_file():
            raise ProviderError(f"$SCIVO_PROVIDERS points at {path}, which is not a file.")
        return path
    for resolve in SEARCH_PATHS:
        candidate = resolve()
        if candidate and candidate.is_file():
            return candidate
    return None


def load_all() -> dict[str, Provider]:
    providers = {BUILTIN_NAME: Provider(name=BUILTIN_NAME, note="the Anthropic API (default)")}
    path = _config_path()
    if not path:
        return providers
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProviderError(f"{path}: {exc}") from exc
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        known = {f for f in Provider.__dataclass_fields__ if f not in {"name", "source"}}
        unknown = set(spec) - known
        if unknown:
            raise ProviderError(
                f"{path}: provider '{name}' has unknown key(s) {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(known))}"
            )
        providers[name] = Provider(name=name, source=str(path), **spec)
    return providers


def get(name: str) -> Provider:
    providers = load_all()
    if name not in providers:
        path = _config_path()
        where = f" (read {path})" if path else " (no providers.toml found)"
        raise ProviderError(
            f"unknown provider '{name}'. Available: {', '.join(sorted(providers))}{where}"
        )
    return providers[name]


EXAMPLE_FILE = Path(__file__).with_name("providers.example.toml")


def example_text() -> str:
    """The shipped skeleton. One copy, so it cannot drift from the docs."""
    return EXAMPLE_FILE.read_text(encoding="utf-8")


def write_example(destination: Path) -> Path:
    """Copy the skeleton next to the project. Never overwrites."""
    if destination.exists():
        raise ProviderError(f"{destination} already exists — not overwriting it.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(example_text(), encoding="utf-8")
    return destination


def probe(provider: Provider, timeout: float = 90.0) -> tuple[bool, str]:
    """Ask the endpoint for one tool call and report what came back.

    A local server that only serves OpenAI chat-completions answers /v1/messages
    with a 404, and one that serves the route but cannot emit `tool_use` fails
    later, mid-task, as "the agent never calls any tool" — which reads as a
    prompt problem. Both are worth finding before a session, not during one.
    """
    import json
    import urllib.error
    import urllib.request

    if not provider.base_url:
        return True, "Anthropic API (not probed)"

    body = json.dumps({
        "model": provider.model or "default",
        "max_tokens": 128,
        "tools": [{
            "name": "ping",
            "description": "Answer a ping.",
            "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}},
                             "required": ["n"]},
        }],
        "messages": [{"role": "user", "content": "Call the ping tool with n=1."}],
    }).encode()
    request = urllib.request.Request(
        provider.base_url.rstrip("/") + "/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": provider.auth_token or "unused",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        hint = " — serves OpenAI chat-completions only? put a LiteLLM proxy in front" \
            if exc.code == 404 else ""
        return False, f"HTTP {exc.code} on /v1/messages{hint}"
    except Exception as exc:  # noqa: BLE001 - connection refused, timeout, bad JSON
        return False, f"{type(exc).__name__}: {exc}"

    if payload.get("type") != "message":
        return False, f"not an Anthropic message (got keys: {', '.join(list(payload)[:5])})"
    kinds = {block.get("type") for block in payload.get("content", [])}
    if "tool_use" not in kinds:
        return False, (f"/v1/messages works but returned {', '.join(sorted(kinds)) or 'nothing'} "
                       "instead of tool_use — the agent loop needs tool calls")
    return True, f"/v1/messages + tool_use ok (model {payload.get('model', '?')})"
