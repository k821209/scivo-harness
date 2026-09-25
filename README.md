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
scivo sessions             # this project's sessions, newest first, with their titles
scivo resume [N | id]      # continue one: its number above, or the start of its id
scivo -c                   # continue the most recent session
```

Sessions are named by the number `scivo sessions` shows or by the first few
characters of the id. `/session` inside a session prints its id, and leaving
prints the command that resumes it.

Resuming prints the last two exchanges of the conversation it is joining, so
you can see which one you got, and it comes back on the model that session was
using — a conversation held with a local model does not resume on Claude.
`--provider` or `--model` overrides that.

Inside a session, `/scivo-control` drives it from a web page — see below.

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
| the same MCP tool called with the same arguments three times in one turn | denied, with the result it already has named; at five the turn is stopped |
| the same Bash command five times in one turn | nudged to `Monitor` with an until-loop for polling — never denied, because a `tail log` in a row is watching a long job, not a stuck loop |

**The tool surface is pruned — for correctness, not for context.** The scivo
server exposes 234 tools. `--profile` drops whole domains, so they are absent
from the model's tool list rather than merely denied:

| profile | domains it carries |
|---|---|
| `full` | paper, materials, analysis, deck, video |
| `paper` | paper, materials, analysis |
| `writing` | paper, materials |
| `deck` | deck, materials |
| `video` | video, materials |
| `analysis` | analysis |
| `--read-only` | any of the above, minus every writing tool |

`materials` is its own domain: uploading a reference image, a clip, a
briefing document is what materials are for, so a video or a deck session
carries them. The video profile no longer drags in the paper tools you do
not need to upload a video. `generate_image` is filed under paper but the
video and deck profiles borrow it: a chunk's keyframes are made with it
before the chunk is generated.

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

## Switching models, inside a session

```
/model                 current model and the choices
/model sonnet          opus · sonnet · haiku · fable — the conversation continues
/model local           a provider from providers.toml — starts a new conversation
/model opus            back to Claude (also a new conversation, if you were on local)
```

Claude models change in place. The same session, with its history, carries on
under the new model. A different provider cannot: the endpoint belongs to the
Claude Code process, so switching restarts it, and scivo says it is starting a
new conversation rather than trying to carry Claude's history onto a local
server. `scivo providers use <name>` makes a provider this project's default.
Local turns show "local model" instead of a price — Claude Code prices even
models it does not know. A claude.ai subscription shows "claude.ai · on your
plan" for the same reason: the CLI's per-turn dollar figure is what an
equivalent API call would cost, and that is not what the plan is billed for.
The dollar figure only appears when the session is API-billed —
`ANTHROPIC_API_KEY` in the environment, or a gateway provider with its own
token.

## When the typed line looks wrong

Inside GNU screen the input line can draw one column off: the cursor eats a
character and the completion menu lands on the text. Only the drawing is
wrong — what you typed still runs. Observed in screen 4.09.01 at ko_KR.utf8,
and not in the macOS Terminal with the same build, so it is the terminal
rather than the harness. If it happens:

```bash
SCIVO_PROMPT_STYLE=list scivo     # completions listed below the line, no floating menu
SCIVO_PROMPT_STYLE=simple scivo   # plain input(): nothing is redrawn at all
```

Everything else works the same; `scivo status` lists the commands the menu
would have offered. Put the one that behaves in your shell profile.

## Permissions, inside a session

By default each tool that changes something asks first: `[y]es / [a]lways /
[n]o` in the terminal, or buttons on the `/scivo-control` page. When a model
fires several calls at once, the questions come one at a time, and answering `a`
on the first settles the rest. You can change this without restarting:

```
/permissions                         show the mode and what is always allowed
/permissions always update_section   stop asking for one tool, this session
/permissions always -update_section  ask for it again
/permissions auto                    a classifier approves or refuses each call
/permissions acceptEdits             file edits run; everything else asks
/permissions plan                    read and plan only
/permissions default                 back to asking
/dangerously-skip-permissions        ask for nothing        (… off to ask again)
```

While asking is off, the prompt reads `scivo[skip]›`, and it shows `[auto]`,
`[edits]` or `[plan]` for the other modes, so you can always see that you are
not in the default. `--permission-mode` sets the same thing at launch.

Two things do not change with the mode:

- **The guardrails still apply.** A raw `ssh … nohup` and a remote `pkill -f`
  are blocked even under `/dangerously-skip-permissions`. They are hooks that
  run before any permission decision.
- **Tools that reach outside the machine are approved one call at a time.** This
  covers YouTube uploads, `publish_page` and remote jobs. `always` refuses them.
  The one way to stop asking about them is `/dangerously-skip-permissions`,
  which says so when you turn it on.

Every session is launched with bypass made *available*, via the CLI's
`--allow-dangerously-skip-permissions`, but not turned on. Without that the CLI
refuses to switch to it mid-session.

## Reading the tool lines

Each tool call is numbered and stamped when it starts, and closed when it
returns:

```
  #7 21:45:27  Bash  ssh node2 'docker exec comfyui-h3 …
  #7 done in 94.2s
  #8 21:47:01  Bash  ssh node2 'docker exec comfyui-h3 …
```

Four identical `ssh` lines in a row used to give no way to tell a call still
running from one the model had reissued. An open number is a call still in
flight. A `blocked after` line (yellow) is a soft refusal the model can
rephrase — a Claude Code policy like `sleep N` chained to a command, or a
permission you declined — and `failed after` (red) is a real error.

When nothing has printed for two seconds, a line keeps count:

```
  ⠹ working 47s  ·  esc to stop
```

It appears while the model thinks and while a slow tool runs, and the next
real output wipes it — so a quiet session is never ambiguous between working
and hung.

## When the model asks you something

Claude Code's question tool expects whatever shows permission prompts to
collect the answers. Nothing here did, so every question came back to the
model as "The user did not answer the questions" and it decided alone — once,
which paper a lecture would be built on. Now the question is asked:

```
  논문 선택: 어느 논문으로 발표를 준비할까요?
    1. A. Meconopsis — SRA 공개데이터라 재현 가능
    2. B. Fagopyrum — 그림이 적음
  a number, your own words, or blank to skip:
```

Type a number, several numbers for a multi-choice question, or your own words
— an answer that is not one of the options is passed through as written.
Blank skips, and the model is told nobody answered. In `scivo run` the tool is
refused with instructions to put the question in its answer instead. While
`/scivo-control` is on, the page says a question is waiting in the terminal.

## Context, inside a session

```
/context      what fills the window right now, by category
/compact      summarise the conversation and carry on with a shorter one
/clear        start a fresh conversation, when the old one no longer fits
```

**Esc stops the turn that is running** — in the terminal, and on the page,
where the Stop button does the same. Keys pressed during a turn are not lost:
they open the next prompt already typed.

Every turn ends with the percentage used, and the line turns yellow as the
auto-compact threshold approaches. Compacting sends the whole conversation to
the model, so a conversation already past the window cannot be compacted out of
it — `/clear` is the way out, and the old conversation stays in
`scivo sessions`.

## Driving a session from the web — `/scivo-control`

Inside a running `scivo`, type `/scivo-control`. It prints a link, and the
page also appears in a dock in the dashboard. Open it on any device you are
signed in on, and the same local session is driven
from there: send messages, watch replies stream in, answer tool approvals with
buttons, and press **Stop** to interrupt a turn. The terminal keeps working at
the same time, and anything typed there shows up on the page too.
`/scivo-control off` unpublishes the page, and so does leaving `scivo`.

```
scivo› /scivo-control

  scivo-control on
  open      https://co-scientist-5af1a.web.app/p/<project>/<pub>
            open it from the dashboard's dock, or this link while signed in
            it opens for you and nobody else — no code to type or forward
```

Leaving the page on a phone and coming back re-opens the live subscriptions
and re-reads the session's heartbeat, so a suspended tab does not come back
reading "disconnected" on a session that is running fine.

A message sent while a turn is running is shown at once and marked queued —
it used to sit as "sending…" for as long as the turn took, which reads as a
lost message. When the page opens it posts the last few exchanges of the session, so a page
opened mid-conversation — or after `scivo -c` — shows what it is joining
rather than an empty log.

**Everything still runs on your machine.** The page is only a window onto it:
the tools, the shell and the files are local, and so is the Claude login.

**Local images render.** A `![](path/to/screenshot.png)` in the model's
reply is inlined as a data URI on the way to the page (up to 200 KB each,
300 KB per message; larger ones are left with a size note). The page runs on
an https origin and cannot fetch file:// paths, so without this a shot the
model referenced was just a broken-image icon.

**The page renders what the model writes.** Markdown comes out with tables,
code highlighting and Korean text, and slash commands complete from a menu
just as they do in the terminal. An `html` code block also gets a live preview.
That preview sits in a `sandbox=""` iframe, so scripts are off and it has no
origin. A lone `~` stays a tilde, because in research text it means
"approximately"; strikethrough needs `~~`.

**It is for one person driving their own session.** The page is published as
`kind="control"`, and the dashboard signs the project owner in from their own
login, stamping every response `reviewer: "owner"` — the only label this side
acts on. Page code cannot forge it; the server puts it there. No passcode is
minted, so there is nothing to type and nothing to forward: anyone else
opening the link has no way in. Codes issued by earlier versions are revoked
the next time `/scivo-control` starts.

Which also means: whoever is signed in to the dashboard on that device can
type into a shell on your machine, on your Claude account. Sign out on a
device you share.

**The page runs in the dashboard's origin, and it can send the agent
instructions.** That combination is why model output is never trusted as
markup. All markdown goes through DOMPurify with `style`, form and embedding
elements removed. HTML is shown only inside the sandboxed preview, and this code
creates that iframe after sanitizing, so model output cannot edit the sandbox
attribute. Without those two steps, a prompt injection that got script onto the
page could write a message the agent would then carry out.

**How it works.** No new server is involved. It uses `publish_page`: the page
reads a head doc and fixed-size transcript chunks, and writes to three fixed
response docs (inbox, approvals, control). The harness reads and writes those
over MCP.

The page **subscribes** to the head and to every transcript chunk that can still
change. It also subscribes one chunk ahead, before that chunk exists, so the
first event of a new chunk arrives without waiting on the head. The harness
cannot subscribe, because it reaches Firestore only through MCP calls, and the
server answers those one at a time in about 60 ms. So it polls the response
docs: every 0.15 s while the session is busy, every 0.5 s after a minute of
quiet. Measured on the live dashboard, median of five runs each:

| | before (polling both ways) | now |
|---|---|---|
| web send → terminal receives | 546 ms | 195 ms |
| web send → confirmed on the page | 1806 ms | 424 ms |
| typed in the terminal → on the page | 959 ms | 187 ms |

A message sent from the page also shows immediately, dimmed, and is replaced
once the session takes it. What remains on the web → terminal path is the
poll interval plus one MCP call. Removing that would need a subscription on the
harness side.

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
`doctor` sends a tool-call request and then a request with a system message
in the middle of the conversation, which is how Claude Code actually talks. It
tells you which case you are in rather than letting it surface mid-task.

### Local model, start to finish

```bash
scivo providers --init                 # writes .scivo/providers.toml; set model to what /v1/models reports
scivo --provider local doctor          # probes the endpoint the way a session will use it
scivo --provider local                 # if doctor is happy
```

Some local chat templates (Qwen3's on llama-server) refuse a message format
Claude Code sends. A session detects that when it connects and fixes it
in-process, with one line saying so, so there is nothing to set up.
`scivo shim --help` explains what it does. A server that is not running is
refused before the session starts, instead of retrying for a minute.

scivo asks a local server for its context window (`/props`) and passes it to
the CLI, which does not know the model and would otherwise guess: a session
that grew past the window failed every turn with "request exceeds the available
context size" instead of compacting. Set `context_tokens` in providers.toml for
a server that reports none.

A local session carries a smaller kit than a Claude one: no project guide in
the prompt, the core file and shell tools, and ~36 scivo tools for reading and
editing a paper, its todos and memory, and running jobs on registered servers.
`--profile video`, `deck` or `analysis` loads that domain's tools on top, which
is how a local session gets `add_video` or the deck tools; the paper domain is
115 tools and stays pruned, its everyday half already being in the core.
A local server re-reads the whole
prompt on every request, with no caching and no deferred tool loading, and the
full session is about 100k tokens (a 27B model sat for minutes before its first
word). The lean one is about 14k. `/model opus` restores everything.

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

**Install it into its own virtualenv.** The Scivo MCP comes with it as a
dependency, and the Agent SDK bundles the Claude Code binary, so this is
everything:

Needs **Python 3.11 or newer** (`python3 --version`). Ubuntu 22.04 ships 3.10,
so install a newer Python there first.

```bash
python3 -m venv ~/.scivo
~/.scivo/bin/pip install "scivo-harness @ git+https://github.com/k821209/scivo-harness.git"
mkdir -p ~/.local/bin && ln -sf ~/.scivo/bin/scivo ~/.local/bin/scivo
```

If `scivo` is then "command not found", `~/.local/bin` is not on your PATH yet.
On Ubuntu, `~/.profile` adds it only when the directory already existed at
login, so a new terminal fixes it. On macOS it is never added, so put this in
`~/.zshrc`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Then, in each project:

```bash
cd /path/to/your/project
scivo setup --key csk_… --project <project id>
```

**Why its own virtualenv, and not just `pip install`.** The first person to
install this ran a bare `pip`, which resolved to a conda environment that was
not the active one — `CONDA_DEFAULT_ENV` said `base` while PATH put an env's
`bin` first. Two things followed, both silent, both reported by pip as success:
a second `scivo` appeared ahead of theirs on PATH, and `co-scientist-local`,
pulled in from git as a dependency, replaced the **editable** install that every
project on that machine was running. A dedicated virtualenv has neither failure
available to it. `scivo setup` says so when it finds itself somewhere shared,
and `scivo doctor` reports a second `scivo` on PATH.

`setup` writes `.mcp.json` (mode 600), adds it to `.gitignore`, links the
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

## Signing in — three ways

scivo never handles credentials. It stores none and sets none, and Claude Code
resolves them the way it does when run by hand. Pick one:

```bash
scivo login               # a Claude subscription (Pro, Max, Team) — opens Claude's own sign-in
scivo login --console     # an Anthropic Console account, billed by API usage
export ANTHROPIC_API_KEY=sk-ant-…   # or an API key in the environment
```

`scivo login` runs Claude Code's own `auth login`, which is where Anthropic
requires sign-in to happen. The command exists because an install of scivo
alone has Claude Code bundled inside the Agent SDK and no `claude` on PATH, so
"run `claude auth login`" was an instruction a new user could not follow.
`scivo logout` signs out. `scivo doctor` shows whether you are signed in. An
`ANTHROPIC_API_KEY` in the environment wins over a stored login (Claude Code's
default; a key present is read as explicit consent to be billed there), and
`doctor` says so when both are set. Pass `scivo --subscription` for a single
run, or `unset ANTHROPIC_API_KEY` in the shell, to fall back to the claude.ai
login without touching the key on disk.

The third way needs no Claude account at all: a local model. See
[Model endpoints](#model-endpoints).

## If you keep a co-scientist-mcp-public checkout

`scivo setup` finds it (at `~/co-scientist-mcp-public`, or wherever
`$CO_SCIENTIST_CHECKOUT` points) and writes a `.mcp.json` that runs the MCP
from it, so `git pull` in the clone updates every project and no pip step has
to be remembered. It checks first that the checkout actually starts under that
interpreter, and falls back to the installed copy — saying why — when it does
not. `scivo update` does the same repair for a project set up earlier.

`scivo update` also runs `git pull` in that clone, because the clone is what
the session runs: updating only the pip copy left every session on the old
code while the MCP kept reporting a newer version available.

`--no-checkout` on either command uses the installed copy instead;
`scivo setup --python PATH` names a different interpreter to run the MCP.

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

## Testing

`pytest tests` — a small suite that stubs the Agent SDK where needed. The
build was exercised with one-off scratch scripts (a pty + `pyte` screen for
the REPL, a mock SSE server, Playwright with a stubbed `window.scivo` for the
control page); pieces of that get codified here as they are touched.

The control page has a browser drive: `node tests/browser/control_page.spec.mjs`
(Playwright + a local Chrome, run from a directory with `playwright` installed)
opens `control_page.html` with a stubbed `window.scivo`, pushes a head and a
transcript chunk with an approval and a question, clicks through them, sends a
message, opens the slash menu, fails a write on purpose, presses Stop, ends the
session, and prints what the page did — `"errors": []` at the end is the pass.
