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

# Credentials the harness never supplies, stores or intermediates. We set none
# of these; the bundled Claude Code binary resolves its own, exactly as it does
# when run by hand. Anthropic's terms require sign-in to complete through their
# own flow, and restricting an authentication method built into the binary is
# not permitted either — so the harness stays out of the way entirely.
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")



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
    context_tokens: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    note: str = ""
    source: str = "built-in"

    @property
    def is_local(self) -> bool:
        return bool(self.base_url)

    def resolve_env(self, *, api_billing: bool = False) -> dict[str, str]:
        """The environment the CLI subprocess runs under.

        `ANTHROPIC_API_KEY` is scrubbed (set empty, which the CLI reads as
        unset) unless the caller asked to be billed per token. resolve_env only
        ever ADDED variables, so a key in the parent shell reached the CLI
        unchanged: with a gateway provider it was sent to that host, and
        `--subscription` — parsed and never used — silently billed the API tab.

        Scrubbing is now the DEFAULT rather than something `--subscription`
        turns on. The premise of the harness is that the customer's claude.ai
        plan pays; an API key that happens to be exported in a shell is not a
        decision to be billed per token, and the moment the subscription is
        refused — which Anthropic's terms now allow for the Agent SDK — a
        present key is exactly what a silent fallback would reach for. Being
        billed by the token is a choice, so it takes a flag (`--api-billing`)
        or a provider that sets the key in its own `env` table, which is
        applied last and therefore still wins."""
        out: dict[str, str] = {}
        if self.base_url or not api_billing:
            for var in API_KEY_VARS:
                out[var] = ""
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


def auth_source(provider: Provider) -> str:
    """What will authenticate this session, as far as we can tell from here.

    Deliberately does not try to detect a stored login: Claude Code keeps it in
    a file on Linux and in the Keychain on macOS, so a file check reports "none"
    on a machine that is perfectly logged in. We report what we set, and leave
    the rest to the CLI.
    """
    if provider.base_url:
        return f"provider token ({provider.name})"
    for var in API_KEY_VARS:
        if os.environ.get(var):
            return f"${var}"
    return "whatever `claude` already uses on this machine"


SETTINGS_FILE = Path(".scivo") / "settings.json"


def chosen_name(root: Path, flag: str | None) -> tuple[str, str]:
    """The provider to use and why: the flag, then the project's choice, then Claude.

    Picking a local model used to mean typing `--provider local` on every run;
    `scivo providers use local` now records it once per project.
    """
    import json

    if flag:
        return flag, "--provider"
    path = root / SETTINGS_FILE
    try:
        name = json.loads(path.read_text(encoding="utf-8")).get("provider")
    except (OSError, ValueError):
        name = None
    if name:
        return name, f"set with `scivo providers use` ({path})"
    return BUILTIN_NAME, "default"


def set_default(root: Path, name: str) -> Path:
    import json

    get(name)  # refuse a name that does not exist, with the usual message
    path = root / SETTINGS_FILE
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if name == BUILTIN_NAME:
        settings.pop("provider", None)
    else:
        settings["provider"] = name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return path


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


def probe(provider: Provider, timeout: float = 180.0) -> tuple[bool, str]:
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
    except TimeoutError:
        # A local server shared with something else, or one reloading the model,
        # looks identical to a dead endpoint here. Say which we cannot tell.
        return False, (f"no response in {timeout:.0f}s — the route answered earlier, so the "
                       "server is likely busy or reloading the model rather than wrong. "
                       "Check it directly: curl " + provider.base_url.rstrip("/") + "/v1/models")
    except Exception as exc:  # noqa: BLE001 - connection refused, bad JSON
        return False, f"{type(exc).__name__}: {exc}"

    if payload.get("type") != "message":
        return False, f"not an Anthropic message (got keys: {', '.join(list(payload)[:5])})"
    kinds = {block.get("type") for block in payload.get("content", [])}
    if "tool_use" not in kinds:
        return False, (f"/v1/messages works but returned {', '.join(sorted(kinds)) or 'nothing'} "
                       "instead of tool_use — the agent loop needs tool calls")

    # Passing the tool call is not enough. Claude Code puts operator notes into
    # messages[] as role "system", and many GGUF chat templates (Qwen3's) raise
    # on that: the probe used to pass and the first real turn then died with
    # "System message must be at the beginning". Ask the same way it will.
    accepted = system_messages_ok(provider.base_url, provider, timeout)
    if accepted is False:
        return False, ("tool_use works, but this server rejects a message format Claude Code "
                       "sends. Sessions fix this themselves by starting the shim; "
                       "`scivo shim --help` explains it.")
    return True, f"/v1/messages + tool_use + system messages ok (model {payload.get('model', '?')})"


def system_messages_ok(base_url: str, provider: Provider, timeout: float = 30.0) -> bool | None:
    """True if the server accepts a mid-conversation system message, False if its
    chat template refuses one, None if it could not be told (slow, unreachable)."""
    import json
    import urllib.error
    import urllib.request

    shaped = json.dumps({
        "model": provider.model or "default", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                     {"role": "system", "content": "Be brief."}, {"role": "user", "content": "say ok"}],
    }).encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/messages", data=shaped,
        headers={"content-type": "application/json", "anthropic-version": "2023-06-01",
                 "x-api-key": provider.auth_token or "unused"})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").lower()
        return False if "system" in detail else None
    except Exception:  # noqa: BLE001
        return None


def reachable(provider: Provider, timeout: float = 5.0) -> None:
    """Refuse a server that is not there, before a session is built on it.

    Otherwise Claude Code retries ten times with backoff — over a minute —
    and the session looks hung. A refused connection is certain; a slow one
    may be a busy server, so only the first stops anything.
    """
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen(provider.base_url.rstrip("/") + "/v1/models", timeout=timeout).close()
    except urllib.error.HTTPError:
        return  # something is answering
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (ConnectionRefusedError, OSError)) and not isinstance(exc.reason, TimeoutError):
            raise ProviderError(f"cannot reach {provider.base_url} ({exc.reason}). "
                                f"Is the '{provider.name}' server running?") from exc
    except (TimeoutError, OSError):
        return


@dataclass
class Endpoint:
    """What a session actually connects to, and anything to tear down after."""

    env: dict[str, str]
    note: str | None = None
    stop: "object | None" = None

    def close(self) -> None:
        if callable(self.stop):
            self.stop()


def context_window(provider: Provider, timeout: float = 5.0) -> int | None:
    """The server's real context size, from its own /props.

    Claude Code does not know a local model, so it assumes a window, compacts
    against that, and the assumption was larger than this server: a session
    grew past 131,072 tokens and every further turn came back as
    "400 request exceeds the available context size". llama.cpp reports the
    number it was started with, so ask instead of assuming.
    """
    if provider.context_tokens:
        return provider.context_tokens
    if not provider.base_url:
        return None
    import json as _json
    import urllib.request

    def ask(path: str):
        try:
            with urllib.request.urlopen(provider.base_url.rstrip("/") + path, timeout=timeout) as response:
                return _json.loads(response.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - any failure just means "do not claim to know"
            return None

    # A slot's own n_ctx first: `--parallel N` splits the server's context
    # between N slots, and a session gets one slot, not the whole thing.
    slots = ask("/slots")
    if isinstance(slots, list):
        sizes = [s.get("n_ctx") for s in slots if isinstance(s, dict) and isinstance(s.get("n_ctx"), int)]
        if sizes:
            return min(size for size in sizes if size > 0)
    props = ask("/props") or {}
    for value in (props.get("n_ctx"),
                  (props.get("default_generation_settings") or {}).get("n_ctx")):
        if isinstance(value, int) and value > 0:
            return value
    return None


def prepare(provider: Provider, timeout: float = 30.0, *, api_billing: bool = False) -> Endpoint:
    """Resolve a provider into environment for Claude Code, fixing what can be fixed.

    A local server whose chat template rejects mid-conversation system messages
    used to need `scivo shim` running in a second terminal and a separate
    provider pointing at it. Choosing `local` without that failed as a string of
    silent retries. Now the check runs here, and when the template refuses, the
    shim starts inside this process and the session is pointed at it.
    """
    env = provider.resolve_env(api_billing=api_billing)
    if not provider.base_url:
        return Endpoint(env)
    reachable(provider)
    window = context_window(provider)
    if window:
        # Leave the reply room inside the window: the error counts the prompt
        # and the generation together, so compacting at the window itself is
        # already too late.
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(max(8192, window - (provider.max_output_tokens or 4096)))
    verdict = system_messages_ok(provider.base_url, provider, timeout)
    if verdict is not False:
        return Endpoint(env)

    from .shim import start_background

    url, stop = start_background(provider.base_url)
    env = {**env, "ANTHROPIC_BASE_URL": url}
    through = system_messages_ok(url, provider, timeout)
    note = ("this server's chat template refuses a message format Claude Code sends; "
            f"scivo is reordering it in-process ({url} → {provider.base_url}). "
            "`scivo shim --help` explains.")
    if through is False:
        note += " It still refuses after reordering — check the server."
    return Endpoint(env, note, stop)

def provider_for_model(model: str) -> str | None:
    """Which configured provider serves this model name, if any."""
    if model.startswith("claude-"):
        return BUILTIN_NAME
    for name, provider in load_all().items():
        if provider.model == model or provider.small_model == model:
            return name
    return None


# What a Pro/Max credential may and may not be used for, and how a session
# identifies itself. All three are facts, not inference:
#
#   · The Agent SDK sets `CLAUDE_CODE_ENTRYPOINT=sdk-py` on the CLI it spawns
#     (claude_agent_sdk/_internal/transport/subprocess_cli.py), so a harness
#     session is identifiable as SDK usage at the source rather than passing
#     for an interactive one.
#   · Anthropic began rejecting subscription OAuth tokens outside Claude Code
#     at the API layer on 2026-01-09, with one message:
#     "This credential is only authorized for use with Claude Code and cannot
#     be used for other API requests."
#   · Its service terms gained an "Authentication and credential use" section
#     on 2026-02-19 saying Free/Pro/Max OAuth tokens may not be used with
#     third-party tools OR the Agent SDK.
#
# The harness runs the official CLI through the SDK, which is why it works at
# all; whether that stays true is Anthropic's call, and the point of reporting
# it is that the day it changes, the session says why instead of just failing.
CREDENTIAL_REFUSAL = "only authorized for use with Claude Code"
SDK_ENTRYPOINT = "sdk-py"


def credential_state() -> dict:
    """What `claude auth status` says, or {} when it cannot be read."""
    import json
    import subprocess

    from .failures import claude_binary

    try:
        done = subprocess.run([claude_binary(), "auth", "status"],
                              capture_output=True, text=True, timeout=30)
        return json.loads(done.stdout)
    except Exception:  # noqa: BLE001 - a report that cannot be made is empty
        return {}


def billing_lines(provider: Provider) -> list[str]:
    """How this session's inference is paid for, as lines to print.

    Short when nothing is in question — an API key or a local endpoint is
    simply what it is. Longer only for the combination that Anthropic's terms
    address: a claude.ai subscription driven through the Agent SDK.
    """
    import os

    if provider.base_url:
        return [f"{provider.name} endpoint — no Anthropic subscription involved"]
    for var in API_KEY_VARS:
        if os.environ.get(var):
            return [f"${var} — metered per token on that account, not a plan"]

    state = credential_state()
    if not state:
        return ["could not read `claude auth status`"]
    if not state.get("loggedIn"):
        return ["not signed in"]
    method = state.get("authMethod")
    plan = state.get("subscriptionType") or "?"
    api_provider = state.get("apiProvider") or "?"
    head = f"{method} login · {plan} · apiProvider={api_provider}"
    if method != "claude.ai":
        return [head]
    return [
        head,
        f"sessions identify as CLAUDE_CODE_ENTRYPOINT={SDK_ENTRYPOINT} (the Agent SDK sets it)",
        "Anthropic's terms (2026-02-19) bar Pro/Max OAuth tokens in third-party "
        "tools and the Agent SDK; API-layer enforcement began 2026-01-09.",
        "Working right now means this path is not being refused — if it starts, "
        f'the error says "{CREDENTIAL_REFUSAL}".',
        "If it does: Scivo itself is unaffected. The MCP and the skills run in "
        "Claude Code directly on the same plan — the harness is one host of "
        "four. `--api-billing` is the other route and is metered per token, so "
        "it is never taken on its own.",
    ]
