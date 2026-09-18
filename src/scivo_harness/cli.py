"""`scivo` — the command line."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import ui
from .compat import check as check_compat, shadowing_binaries
from .config import ConfigError, find_root, load
from .failures import explain
from .preflight import ProjectMismatch, to_markdown
from .providers import (
    chosen_name,
    set_default,
    EXAMPLE_FILE,
    ProviderError,
    auth_source,
    example_text,
    get as get_provider,
    load_all,
    probe,
    write_example,
)
from .scivo_mcp import connect
from .sessions import SessionLookupError, latest as latest_session, resolve as resolve_session, rows as session_rows
from .setup import (
    Result,
    SetupError,
    check_mcp_importable,
    ensure_claude_md,
    environment_note,
    ensure_gitignore,
    link_skills as link_setup_skills,
    roll_back,
    write_mcp_json,
)
from .update import (
    apply as apply_update,
    inspect as inspect_install,
    has_remote,
    checkout_for_mcp,
    drop_inapplicable_warning,
    link_skills,
    repo_root,
    restore_editable,
)
from .session import DEFAULT_EFFORT, build, survey
from .toolsets import PROFILES, plan as make_plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scivo", description="A command-line harness for the Scivo research co-scientist."
    )
    parser.add_argument("--profile", default="full", choices=sorted(PROFILES),
                        help="which domains of the scivo tool surface to load (default: full)")
    parser.add_argument("--read-only", action="store_true",
                        help="load no writing tools at all")
    parser.add_argument("--provider", default=None,
                        help="model endpoint for this run (default: the project's choice, else anthropic)")
    parser.add_argument("--model", default=None,
                        help="override the provider's model")
    parser.add_argument("--effort", default=DEFAULT_EFFORT,
                        choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--add-dir", action="append", default=[], metavar="PATH",
                        help="let the session reach this directory too; repeatable")
    parser.add_argument("--permission-mode", default="default",
                        choices=["default", "acceptEdits", "plan", "dontAsk",
                                 "bypassPermissions", "auto"],
                        help="default asks you (interactive) or explains why it cannot (run)")
    parser.add_argument("--no-guide", action="store_true",
                        help="skip the project guide in the system prompt")
    parser.add_argument("--budget", type=float, default=None, metavar="USD",
                        help="stop the session when spend reaches this")
    parser.add_argument("--resume", metavar="SESSION",
                        help="resume a session: its number in `scivo sessions`, or the start of its id")
    parser.add_argument("-c", "--continue", dest="continue_last", action="store_true",
                        help="resume the most recent session in this project")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("chat", help="interactive session (default)")
    sub.add_parser("status", help="run the session-start protocol and print it; no model call")
    sub.add_parser("sessions", help="list this project's sessions, most recent first")
    login = sub.add_parser("login", help="sign in to Claude (runs Claude Code's own login)",
                           add_help=False)
    login.add_argument("rest", nargs=argparse.REMAINDER)
    logout = sub.add_parser("logout", help="sign out of Claude", add_help=False)
    logout.add_argument("rest", nargs=argparse.REMAINDER)
    resume = sub.add_parser("resume", help="pick a session from the list and continue it")
    resume.add_argument("target", nargs="?", help="row number or id prefix; omit to choose from the list")
    sub.add_parser("doctor", help="check the wiring")
    sub.add_parser("tools", help="show what each profile loads")
    providers = sub.add_parser("providers", help="list configured model endpoints")
    providers.add_argument("--init", action="store_true",
                           help="write the tokenless skeleton to .scivo/providers.toml")
    providers.add_argument("--show-example", action="store_true",
                           help="print the skeleton")
    providers.add_argument("action", nargs="?", choices=["use"],
                           help="`use <name>`: make that endpoint this project's default")
    providers.add_argument("name", nargs="?")
    setup = sub.add_parser("setup", help="wire this directory to a Scivo project")
    setup.add_argument("--key", help="the project API key (else $CO_SCIENTIST_API_KEY, else prompt)")
    setup.add_argument("--project", help="expected project id; setup fails if the key binds elsewhere")
    setup.add_argument("--force", action="store_true", help="replace an existing .mcp.json")

    update = sub.add_parser("update", help="update scivo and the Scivo MCP, and re-link skills")
    update.add_argument("--check", action="store_true",
                        help="report what would happen; change nothing")
    update.add_argument("--no-self", action="store_true",
                        help="update only the MCP, leaving scivo itself alone")
    update.add_argument("--restore-editable", action="store_true",
                        help="if the MCP is a snapshot over a source checkout, point it back at the checkout")
    shim = sub.add_parser(
        "shim", help="standalone message-reordering proxy for a local model server (sessions start one themselves)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
You normally never run this: a session on a local provider checks the server
and, when it needs one, starts this proxy inside scivo by itself.

What it fixes. scivo runs on Claude Code, and Claude Code sends one thing some
local model servers refuse: a "system" message in the middle of the
conversation. Qwen3's chat template on llama-server, for one, accepts system
messages only at the start and fails the whole request with

  System message must be at the beginning

The proxy moves each such message into the next user message and passes
everything else through unchanged, streaming included. The model is the same;
only the order changes.

Run it by hand only to put a fixed address in front of such a server for some
other tool:

  scivo shim --upstream http://localhost:8190 --port 8191""")
    shim.add_argument("--upstream", default="http://localhost:8190",
                      help="the local model server (default: %(default)s)")
    shim.add_argument("--port", type=int, default=8191,
                      help="where the shim listens; point a provider's base_url here (default: %(default)s)")
    shim.add_argument("--verbose", action="store_true", help="log each request it forwards")
    run = sub.add_parser("run", help="one prompt, non-interactive")
    run.add_argument("prompt", nargs="+")
    return parser


async def _status(args) -> int:
    config = load()
    briefing, names, _ = await survey(config, with_guide=False)
    print(to_markdown(briefing))
    print("\n" + make_plan(names, args.profile, args.read_only).summary())
    return 1 if briefing.errors else 0


async def _doctor(args) -> int:
    config = load()
    shadows = shadowing_binaries()
    if shadows:
        print(ui.red(f"scivo        {len(shadows)} copies on PATH — the first one runs:"))
        for index, path in enumerate(shadows):
            print(f"             {'→' if index == 0 else ' '} {path}")
        print(ui.dim("             Uninstall the ones you do not want, or call one by full path."))
    provider = get_provider(args.provider)
    print(f"config      {config.source}")
    print(f"provider    {provider.name} → {provider.base_url or 'api.anthropic.com'}"
          f"   {ui.dim(args.provider_reason)}")
    print(f"auth        {auth_source(provider)}")
    if not provider.is_local:
        print(f"login       {_login_state()}")
    if provider.is_local:
        ok, detail = probe(provider)
        print(f"endpoint    {ui.green(detail) if ok else ui.red(detail)}")
    print(f"root        {config.root}")
    print(f"mcp command {config.command} {' '.join(config.args)}")
    print(f"api key     {'set' if config.api_key else ui.red('MISSING')}")
    print(f"CLAUDE.md   project id {config.expected_project_id or ui.yellow('not found')}")
    async with connect(config) as client:
        who = await client.call("whoami")
        if not who:
            print(ui.red(f"whoami      failed: {who.error}"))
            return 1
        identity = drop_inapplicable_warning(config, who.first or {})
        bound = identity.get("project_id")
        match = bound == config.expected_project_id if config.expected_project_id else None
        mark = ui.green("match") if match else (ui.red("MISMATCH") if match is False else "unverified")
        print(f"bound to    {bound} ({mark})")
        print(f"install     {identity.get('install_mode')} {identity.get('installed_version')} "
              f"@ {identity.get('git_sha')}")
        if identity.get("update_available"):
            print(ui.yellow(f"update      {identity.get('latest_version')} available"))
        if identity.get("install_warning"):
            print(ui.red(f"install     {identity['install_warning']}"))
        tools = await client._session.list_tools()  # noqa: SLF001
        print(f"tools       {len(tools.tools)} exposed")
        compatibility = check_compat(tools.tools)
        line = compatibility.report()
        print(f"compat      {ui.green(line) if compatibility.ok else ui.red(line)}")
        if not compatibility.ok:
            return 1
    return 0 if match is not False else 1


async def _identity(config) -> dict:
    """whoami, read from a FRESH server process."""
    async with connect(config) as client:
        who = await client.call("whoami")
        return who.first if who and isinstance(who.first, dict) else {}


async def _sha(config) -> tuple[str, str]:
    identity = await _identity(config)
    return str(identity.get("git_sha", "?")), str(identity.get("installed_version", "?"))


async def _setup(args) -> int:
    import getpass
    import os
    from pathlib import Path

    root = Path.cwd()
    key = args.key or os.environ.get("CO_SCIENTIST_API_KEY")
    if not key and sys.stdin.isatty():
        key = getpass.getpass("Project API key (from the dashboard Setup tab): ").strip()
    if not key:
        print(ui.red("No key. Pass --key, or set CO_SCIENTIST_API_KEY."), file=sys.stderr)
        return 2

    result = Result()
    check_mcp_importable()
    shared = environment_note()
    mcp_path, backup = write_mcp_json(root, key, args.force, result)
    ensure_gitignore(root, result)
    link_setup_skills(root, result)

    # Verify against a real server before claiming the directory is set up.
    config = load(root)
    async with connect(config) as client:
        who = await client.call("whoami")
        if not who:
            undone = roll_back(mcp_path, backup)
            print(ui.red(f"the MCP did not start: {who.error}\n{undone}"), file=sys.stderr)
            return 1
        identity = who.first or {}

    bound = str(identity.get("project_id", "?"))
    if args.project and args.project != bound:
        undone = roll_back(mcp_path, backup)
        print(ui.red(
            f"That key binds to project {bound}, not {args.project}.\n{undone}\n"
            "Take the key and the id from the SAME project's Setup tab."), file=sys.stderr)
        return 1

    ensure_claude_md(root, bound, str(identity.get("project_name") or bound), result)

    for step in result.steps:
        print(f"  {ui.green('✓')} {step}")
    for warning in result.warnings:
        print(f"  {ui.yellow('!')} {warning}")
    if shared:
        print()
        for index, line in enumerate(shared.splitlines()):
            print(ui.yellow(f"  ! {line}") if index == 0 else ui.dim(f"  {line}"))
    print()
    print(f"project     {identity.get('project_name')} ({bound})")
    print(f"mcp         {identity.get('installed_version')} @ {identity.get('git_sha')}")
    print()
    print("Next: " + ui.bold("scivo") + ui.dim("   (or `scivo status` to see the briefing first)"))
    return 0


async def _update_one(install, check: bool) -> tuple[bool, bool]:
    """Returns (ok, moved). Prints its own progress."""
    label = install.dist
    if install.error:
        print(f"  {label:20} {ui.red(install.error.splitlines()[-1][:90])}")
        return False, False
    if not install.found:
        print(f"  {label:20} {ui.dim('not installed here')}")
        return True, False

    kind = "editable" if install.editable else (install.url or "unrecorded source")
    # An editable install reports the version frozen when it was installed —
    # 0.0.1 here while the server actually runs 0.1.20260911. Printing it as if
    # it were current is how that trap gets believed.
    version = f"{install.version} (frozen at install)" if install.editable else install.version
    stamp = f" @ {install.commit_id[:8]}" if install.commit_id else ""
    print(f"  {label:20} {version}{stamp}  {ui.dim(kind)}")

    if check:
        if install.editable:
            root = repo_root(str(install.source_path))
            if root is None:
                how = "no git checkout above it — skip"
            elif not has_remote(root):
                how = f"{root} has no remote — skip"
            else:
                how = f"git pull --ff-only in {root}"
        else:
            how = f"pip install --upgrade --force-reinstall --no-deps {install.requirement}"
        print(ui.dim(f"  {'':20} would run: {how}"))
        return True, False

    ok, output = apply_update(install)
    for line in output.splitlines()[-4:]:
        print(ui.dim(f"  {'':20} {line[:100]}"))
    if not ok:
        print(f"  {'':20} {ui.red('failed')}")
        return False, False

    after = inspect_install(install.interpreter, install.dist)
    moved = after.fingerprint != install.fingerprint
    return True, moved


async def _restore_mcp(install, args) -> tuple[bool, bool]:
    checkout = checkout_for_mcp()
    print(f"  {'co-scientist-local':20} {ui.red('snapshot over a source checkout — not reinstalling the snapshot')}")
    if checkout is None:
        print(ui.dim(f"  {'':20} no checkout found at $CO_SCIENTIST_CHECKOUT or ~/co-scientist-mcp-public"))
        return False, False
    command = f"{install.interpreter} -m pip install -e {checkout}/apps/local-mcp --no-deps"
    if args.check or not args.restore_editable:
        print(ui.dim(f"  {'':20} other projects using {install.interpreter} run this copy too."))
        print(ui.dim(f"  {'':20} to point it back at the checkout:  scivo update --restore-editable"))
        print(ui.dim(f"  {'':20} (runs: git pull in {checkout}, then {command})"))
        return True, False
    ok, output = restore_editable(install, checkout)
    for line in output.strip().splitlines()[-4:]:
        print(ui.dim(f"  {'':20} {line[:100]}"))
    after = inspect_install(install.interpreter, install.dist)
    if not ok or not after.editable:
        print(f"  {'':20} {ui.red('restore failed')}")
        return False, False
    print(f"  {'':20} {ui.green('editable again → ' + str(checkout))}")
    return True, True


async def _update(args) -> int:
    config = load()
    targets = [("co-scientist-local", config.command)]
    if not args.no_self:
        targets.append(("scivo-harness", sys.executable))

    print(f"project     {config.root}")
    before_sha, before_version = await _sha(config)
    print(f"mcp session {before_version} @ {before_sha}")
    print()

    warning = drop_inapplicable_warning(config, await _identity(config)).get("install_warning")

    failed = False
    moved_any = False
    for dist, interpreter in targets:
        install = inspect_install(interpreter, dist)
        # Only in a shared environment. A dedicated venv such as ~/.scivo is
        # meant to hold a snapshot; guarding it — on the strength of a warning
        # that fires whenever a clone sits on disk — froze its MCP at an old
        # version and told the user to make it editable.
        if (dist == "co-scientist-local" and install.found and not install.editable
                and warning and not install.dedicated_venv):
            # The MCP says it is a snapshot sitting over a source checkout —
            # the silent flip. Reinstalling the snapshot, which is what an
            # "update" of a git install does, would entrench exactly the state
            # the warning asks to undo. It did, once.
            ok, moved = await _restore_mcp(install, args)
        else:
            ok, moved = await _update_one(install, args.check)
        failed = failed or not ok
        moved_any = moved_any or moved

    if args.check:
        return 0

    linked, link_output = link_skills(config)
    print(ui.dim(f"  {'skills':20} {'re-linked' if linked else link_output[:90]}"))

    after_sha, after_version = await _sha(config)
    print()
    print(f"mcp session {after_version} @ {after_sha}")
    if failed:
        print(ui.red("one or more components failed to update."))
        return 1
    # pip prints success whether or not anything moved, so the verdict comes
    # from re-reading the installs and a fresh server process, not from pip.
    if moved_any or (after_sha, after_version) != (before_sha, before_version):
        print(ui.green("updated. Restart any running scivo session to pick it up."))
    else:
        print(ui.yellow("unchanged — everything was already current."))
    return 0


async def _shim(args) -> int:
    from .shim import serve

    serve(args.upstream, args.port, args.verbose)
    return 0


async def _providers(args) -> int:
    from pathlib import Path

    if args.show_example:
        print(example_text(), end="")
        return 0
    if args.init:
        written = write_example(Path.cwd() / ".scivo" / "providers.toml")
        print(f"wrote {written}")
        print(ui.dim("  edit the REPLACE-ME values, then: scivo providers use local && scivo doctor"))
        return 0
    if args.action == "use":
        if not args.name:
            print(ui.red("which one? scivo providers use <name>"), file=sys.stderr)
            return 2
        written = set_default(find_root(), args.name)
        print(ui.green(f"this project now uses {args.name}") + ui.dim(f"   ({written})"))
        if args.name != "anthropic":
            print(ui.dim(f"  check it: scivo doctor   ·   back to Claude: scivo providers use anthropic"))
        return 0

    providers = load_all()
    for name, provider in sorted(providers.items()):
        mark = ui.green("*") if name == args.provider else " "
        target = provider.base_url or "api.anthropic.com"
        print(f" {mark} {ui.bold(name):<24} {target}")
        detail = [f"model {provider.model}" if provider.model else "model: harness default"]
        if not provider.supports_effort:
            detail.append("no effort/thinking")
        if provider.auth_token_env:
            detail.append(f"token from ${provider.auth_token_env}")
        print(ui.dim(f"     {' · '.join(detail)}"))
        if provider.note:
            print(ui.dim(f"     {provider.note}"))
        print(ui.dim(f"     from {provider.source}"))
    if len(providers) > 1:
        print(ui.dim("\n  * = what this project uses.  Change it: scivo providers use <name>"))
    if len(providers) == 1:
        print(ui.dim("\nNo providers.toml found. To add a local endpoint:"))
        print(ui.dim("  scivo providers --init          write .scivo/providers.toml"))
        print(ui.dim("  scivo providers --show-example  print it first"))
        print(ui.dim(f"  skeleton: {EXAMPLE_FILE}"))
    return 0


async def _tools(args) -> int:
    config = load()
    _, names, _ = await survey(config, with_guide=False)
    for profile in sorted(PROFILES):
        print(make_plan(names, profile).summary())
    print(make_plan(names, args.profile, read_only=True).summary())
    return 0


def _print_sessions(root, limit: int = 20) -> list:
    listed = session_rows(root, limit=limit)
    if not listed:
        print(ui.dim(f"No sessions recorded for {root} yet."))
        return listed
    for row in listed:
        print(f"  {ui.bold(str(row.index)):>4}  {ui.dim(row.session_id[:8])}  {row.when}  {row.label}")
    return listed


def _auth(subcommand: str, rest: list[str]) -> int:
    """Hand the terminal to Claude Code's own `auth` flow.

    scivo does not implement sign-in and must not: Anthropic requires it to
    complete in Claude Code. What it adds is finding the binary — a user who
    installed only scivo has Claude Code bundled inside the Agent SDK and no
    `claude` on PATH, so "run claude auth login" was an instruction they could
    not follow.
    """
    import os

    from .failures import claude_binary

    binary = claude_binary()
    argv = [binary, "auth", subcommand, *rest]
    print(ui.dim(f"→ {' '.join(argv)}"), flush=True)
    os.execv(binary, argv)
    return 0  # not reached


def _login_state() -> str:
    import json
    import os
    import subprocess

    from .failures import claude_binary

    try:
        result = subprocess.run([claude_binary(), "auth", "status"], capture_output=True,
                                text=True, timeout=30)
        state = json.loads(result.stdout)
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({type(exc).__name__})"
    text = (f"signed in ({state.get('authMethod')})" if state.get("loggedIn")
            else "not signed in — scivo login")
    if os.environ.get("ANTHROPIC_API_KEY"):
        # auth status reports the stored login even when an API key in the
        # environment is what will actually be used.
        text += " · ANTHROPIC_API_KEY is set and takes precedence"
    return text


async def _sessions(args) -> int:
    config = load()
    _print_sessions(config.root)
    print(ui.dim("\n  scivo resume <number>   ·   scivo -c for the most recent"))
    return 0


async def _resume(args) -> int:
    config = load()
    target = args.target
    if not target:
        listed = _print_sessions(config.root)
        if not listed:
            return 1
        if not sys.stdin.isatty():
            print(ui.red("Name a session: scivo resume <number>"), file=sys.stderr)
            return 2
        target = input(ui.cyan("\n  resume which? ")).strip()
        if not target:
            return 0
    args.resume = target
    return await _chat(args)


async def _chat(args, prompt_text: str | None = None) -> int:
    config = load()
    resume_id = resolve_session(config.root, args.resume) if args.resume else None
    continue_last = False
    if args.continue_last and not resume_id:
        newest = latest_session(config.root)
        if newest is None:
            print(ui.dim("No earlier session in this project — starting a new one."))
        else:
            continue_last = True
            resumed_id = newest
    session = await build(
        config,
        profile=args.profile,
        read_only=args.read_only,
        provider=args.provider,
        model=args.model,
        effort=args.effort,
        with_guide=not args.no_guide,
        interactive=prompt_text is None,
        permission_mode=args.permission_mode,
        resume=resume_id,
        continue_last=continue_last,
        max_budget_usd=args.budget,
        add_dirs=args.add_dir,
    )
    if session.resumed is None and continue_last:
        session.resumed = resumed_id
    if prompt_text is not None:
        from claude_agent_sdk import AssistantMessage, TextBlock, query

        if session.endpoint is not None and session.endpoint.note:
            print(ui.dim(f"· {session.endpoint.note}"), file=sys.stderr)
        async for message in query(prompt=prompt_text, options=session.options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
        print()
        return 0

    from .repl import Repl

    await Repl(session).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if raw[:1] in (["login"], ["logout"]):
        return _auth(raw[0], raw[1:])
    args = _parser().parse_args(argv)
    try:
        args.provider, args.provider_reason = chosen_name(find_root(), args.provider)
    except Exception:  # noqa: BLE001 - resolution problems surface where the provider is used
        args.provider, args.provider_reason = args.provider or "anthropic", "default"
    handlers = {
        "status": _status,
        "doctor": _doctor,
        "tools": _tools,
        "providers": _providers,
        "shim": _shim,
        "update": _update,
        "setup": _setup,
        "sessions": _sessions,
        "resume": _resume,
        "run": lambda a: _chat(a, " ".join(a.prompt)),
        "chat": _chat,
        None: _chat,
    }
    try:
        return asyncio.run(handlers[args.command](args))
    except (ConfigError, ProjectMismatch, ProviderError, SetupError, SessionLookupError) as exc:
        print(ui.red(str(exc)), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - the CLI's failures arrive as these
        message = explain(exc)
        if message is None:
            raise
        print(ui.red(message), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
