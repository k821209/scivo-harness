# scivo-harness

A command-line harness for the Scivo research co-scientist, built on the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview).

```bash
scivo setup --key csk_…    # wire this directory to a Scivo project
scivo                      # interactive session
scivo status               # the session-start protocol, printed. No model call, no cost
scivo doctor               # check the wiring
scivo tools                # what each tool profile loads
scivo run "..."            # one prompt, non-interactive
scivo providers            # configured model endpoints
scivo update               # update scivo + the MCP, re-link skills
```

## Why this exists

A Scivo project already works under a general coding agent: an MCP server, 28
skills, and a `CLAUDE.md` that asks the agent to follow a protocol. The protocol
is the problem. `project_guide` states it as a numbered list and then says, in
several places, that it is the step most often skipped — because a request to a
model is not a property of the session.

This harness makes four of those things structural instead.

**The session-start protocol runs before the first token.** `preflight.py`
speaks MCP directly — `whoami`, project memory, the playbook, decisions, every
paper's open comments and triage and requirement violations and citation
findings, the servers registry, datasets, open to-dos — and the results go into
the system prompt as facts. The model does not decide whether to look. A
project-id mismatch between `CLAUDE.md` and `.mcp.json` stops the session rather
than producing a warning nobody reads.

**Rules that were prose become hooks.** Each one in `guardrails.py` corresponds
to a failure the guide documents as having actually happened:

| Rule | Action |
|---|---|
| `ssh … nohup` — a remote job with no run record | denied, with `submit_remote_job` named |
| `pgrep -f` / `pkill -f` over ssh — the pattern matches the ssh line carrying it | denied, with the bracketed-pattern fix |
| a memory write containing an IP, GPU model, core count or conda env | held for the user — machines belong in the servers registry |
| the second analysis-shaped shell command in a session | a reminder that this is where provenance is lost |

**The tool surface is pruned — for correctness, not for context.** The scivo
server exposes 234 tools. `--profile` drops whole domains, so they are absent
from the model's tool list rather than merely denied:

| profile | scivo tools |
|---|---|
| `full` | 234 |
| `paper` | 201 |
| `deck` / `video` | 161 / 160 |
| `writing` | 144 |
| `analysis` | 92 |
| `--read-only` | 93 (no writing tool at all) |

Measured, so the claim is not guesswork: the prefix costs **33,188 tokens at
`full` and 31,263 at `analysis`** — dropping 142 tools saves about 1,900, not the
28,800 their raw schemas weigh. The CLI already defers MCP tool schemas behind
its own tool search, so pruning is not a context lever. What it buys is a
smaller wrong-tool surface, and `--read-only` buys a guarantee: a session that
cannot write is one that cannot claim to have written.

A tool matching no known domain is kept, so a tool added upstream is never
dropped silently.

**There is no other bug channel.** "개발자한테 보내줘" can only mean
`report_feedback`, because nothing else is wired in. A report meant for Scivo
once went to Anthropic and the user found out only when it never appeared in the
Feedback tab.

## Model endpoints

The Agent SDK drives the `claude` CLI, and the CLI reads its endpoint from the
environment, which `ClaudeAgentOptions.env` controls. So a provider is a named
bundle of environment variables — no fork, no patch.

```bash
scivo providers --init          # writes .scivo/providers.toml from the skeleton
scivo providers --show-example  # print the skeleton first
scivo --provider local doctor   # probes the endpoint before you rely on it
scivo --provider local
```

```toml
[local]
base_url  = "http://localhost:8190"
model     = "Qwen3.8-27B-fast"
small_model = "Qwen3.8-27B-fast"   # the CLI's own small calls go here too
auth_token = "unused"              # local servers ignore it; the CLI needs a value
supports_effort = false            # effort/thinking are Anthropic-model features
max_output_tokens = 8192
```

`.scivo/` is gitignored, because `providers.toml` can carry a token. The
tokenless skeleton lives in the package
(`src/scivo_harness/providers.example.toml`) as the only copy, so it cannot
drift from these docs; `--init` writes it out. For a real secret use
`auth_token_env` and keep the value in the environment.

Config is read from `$SCIVO_PROVIDERS`, then `./.scivo/providers.toml`, then
`~/.config/scivo/providers.toml`. A `$SCIVO_PROVIDERS` that does not exist is an
error, not a fallback — a typo there would otherwise load another project's
endpoints with nothing to read.

The endpoint must serve Anthropic `/v1/messages` **including `tool_use`** — an
agent loop is tool calls. Recent llama.cpp `llama-server` does this natively
(verified here against Qwen3.8-27B); a server offering only OpenAI
chat-completions needs a translator such as the LiteLLM proxy in front.
`doctor` sends one tool-call request and tells you which case you are in, rather
than letting it surface mid-task as "the agent never calls any tool".

### The chat-template trap

A local server can pass that probe and still fail on turn one:

```
API Error: 500 — Jinja Exception: System message must be at the beginning.
```

Claude Code sends operator instructions as `{"role": "system"}` entries inside
`messages[]` rather than editing the top-level `system` field, because that
keeps the cached prefix intact. Qwen3's stock GGUF chat template raises on it.
The error names a template line number and reads like a broken harness.

```bash
scivo shim --upstream http://localhost:8190 --port 8191   # then point base_url at :8191
```

Verified end to end: Qwen3.8-27B on a local `llama-server` completes a scivo
session through it, answering from the preflight briefing. Two non-fatal
warnings are expected — `unrecognized_model` on the CLI's own title call, and a
notice that claude.ai connectors are off because the provider sets an auth
token.

The shim folds each such message into the adjacent user turn — the following
one where there is one, so the instruction still precedes the turn it governs,
and the preceding one otherwise, since two user messages in a row trades one
template error for another. It is a workaround and it does blur a turn-scoped
note into user text; starting `llama-server` with a template that tolerates
system messages anywhere is cleaner when restarting that server is free.

## Design notes

- **Start from the host's prompt, not from scratch.** The system prompt is
  `{"type": "preset", "preset": "claude_code", "append": …}`; the append half is
  assembled conditionally — a section about papers is noise in a project with
  none.
- **Prefix order is cache order.** Charter and guide (stable) first, the
  per-session briefing last.
- **The 28 skills load unmodified.** `setting_sources` + `skills="all"` read the
  project's `.claude/` exactly as Claude Code does.
- **The briefing is a structured brief, not compaction.** For long work, a fresh
  session that starts from `scivo status` beats a compacted one.

## Layout

| file | |
|---|---|
| `config.py` | reads the same `.mcp.json` the Setup tab writes |
| `scivo_mcp.py` | direct MCP client; every call returns an outcome, never raises |
| `preflight.py` | the session-start protocol + the briefing it renders |
| `toolsets.py` | tool profiles |
| `guardrails.py` | the hooks |
| `prompt.py` | system prompt assembly |
| `session.py` | `ClaudeAgentOptions` |
| `repl.py` / `ui.py` / `cli.py` | the terminal |

## Install

One command. The Scivo MCP is a dependency, and the Agent SDK bundles the Claude
Code binary, so nothing else is fetched by hand:

```bash
pip install "scivo-harness @ git+https://github.com/k821209/scivo-harness.git"
cd /path/to/your/project
scivo setup --key csk_… --project <project id>
```

`setup` writes `.mcp.json` (mode 600), adds it to `.gitignore`, links the 28
skills, and then **connects and checks** that the key binds to the project you
named — a mismatch fails here rather than surfacing as a confusing warning in
some later session. It refuses to overwrite an existing `.mcp.json` without
`--force`, and if verification fails after `--force` it puts the old one back
rather than leaving you with a `.bak`.

It does not search for a Python that can import the MCP. The dashboard's shell
script spends twenty lines on that, because it writes `python3` into
`.mcp.json` and PATH resolves differently under another shell or conda
environment — "it worked yesterday", with nothing to read. Here the MCP is a
dependency of the harness, so the interpreter is the one running `setup`.

**Authentication is not ours.** The harness sets no credentials and stores none;
the bundled Claude Code binary resolves its own, exactly as it does when run by
hand. Sign in with `claude auth login` — Anthropic requires it to happen there —
or export `ANTHROPIC_API_KEY`, or point `--provider` at a local endpoint.

## Updating

```bash
scivo update            # scivo, the MCP, and the skills
scivo update --check    # say what would happen; change nothing
```

It asks each interpreter how its distribution was installed, because the two
kinds fail differently and both are documented as having bitten someone: `pip
install --upgrade` on an editable install is a no-op that prints success, and a
git URL whose version did not change reads as "already satisfied". So it runs
`git pull` for one and `--force-reinstall` for the other, then re-reads the
version, the `git_sha` from a fresh server process, and the commit pip recorded.
pip's own output is not evidence that anything moved.

## Not done yet

- Streaming is block-level on the fallback path; partial deltas are used when
  the CLI emits them.
- No token/cost telemetry beyond the per-turn line.
- `--profile` is static for the session; the tools cannot be reloaded mid-session.
- Guardrail coverage is four rules. The guide documents more that could be hooks.
- The shim is HTTP/1.1 chunked passthrough on the stdlib server — fine for one
  local session, not a load-bearing proxy.

## License

MIT — see [LICENSE](LICENSE).

The Claude Agent SDK it builds on, and the Claude Code binary that SDK bundles,
are Anthropic's and carry their own terms: use is governed by Anthropic's
[Commercial Terms of Service](https://www.anthropic.com/legal/commercial-terms),
including when it powers something you make available to others. Two conditions
this harness is built to respect — it does not use the Claude Code name or
imitate its interface, and it neither supplies, stores nor intermediates
anyone's credentials: sign-in happens in Claude Code itself, which is where
Anthropic requires it to happen.
