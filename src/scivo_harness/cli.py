"""`scivo` — the command line."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import ui
from .config import ConfigError, load
from .failures import explain
from .preflight import ProjectMismatch, to_markdown
from .providers import (
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
from .update import (
    apply as apply_update,
    inspect as inspect_install,
    has_remote,
    link_skills,
    repo_root,
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
    parser.add_argument("--provider", default="anthropic",
                        help="model endpoint from providers.toml (default: anthropic)")
    parser.add_argument("--model", default=None,
                        help="override the provider's model")
    parser.add_argument("--effort", default=DEFAULT_EFFORT,
                        choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--no-guide", action="store_true",
                        help="skip the project guide in the system prompt")
    parser.add_argument("--budget", type=float, default=None, metavar="USD",
                        help="stop the session when spend reaches this")
    parser.add_argument("--resume", metavar="SESSION_ID")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("chat", help="interactive session (default)")
    sub.add_parser("status", help="run the session-start protocol and print it; no model call")
    sub.add_parser("doctor", help="check the wiring")
    sub.add_parser("tools", help="show what each profile loads")
    providers = sub.add_parser("providers", help="list configured model endpoints")
    providers.add_argument("--init", action="store_true",
                           help="write the tokenless skeleton to .scivo/providers.toml")
    providers.add_argument("--show-example", action="store_true",
                           help="print the skeleton")
    update = sub.add_parser("update", help="update scivo and the Scivo MCP, and re-link skills")
    update.add_argument("--check", action="store_true",
                        help="report what would happen; change nothing")
    update.add_argument("--no-self", action="store_true",
                        help="update only the MCP, leaving scivo itself alone")
    shim = sub.add_parser(
        "shim", help="proxy a local server whose chat template rejects mid-conversation system messages")
    shim.add_argument("--upstream", default="http://localhost:8190")
    shim.add_argument("--port", type=int, default=8191)
    shim.add_argument("--verbose", action="store_true")
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
    provider = get_provider(args.provider)
    print(f"config      {config.source}")
    print(f"provider    {provider.name} → {provider.base_url or 'api.anthropic.com'}"
          f" ({provider.source})")
    print(f"auth        {auth_source(provider)}")
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
        identity = who.first or {}
        bound = identity.get("project_id")
        match = bound == config.expected_project_id if config.expected_project_id else None
        mark = ui.green("match") if match else (ui.red("MISMATCH") if match is False else "unverified")
        print(f"bound to    {bound} ({mark})")
        print(f"install     {identity.get('install_mode')} {identity.get('installed_version')} "
              f"@ {identity.get('git_sha')}")
        if identity.get("update_available"):
            print(ui.yellow(f"update      {identity.get('latest_version')} available"))
        tools = await client._session.list_tools()  # noqa: SLF001
        print(f"tools       {len(tools.tools)} exposed")
    return 0 if match is not False else 1


async def _sha(config) -> tuple[str, str]:
    """git_sha + version, read from a FRESH server process."""
    async with connect(config) as client:
        who = await client.call("whoami")
        identity = who.first if who and isinstance(who.first, dict) else {}
        return str(identity.get("git_sha", "?")), str(identity.get("installed_version", "?"))


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


async def _update(args) -> int:
    config = load()
    targets = [("co-scientist-local", config.command)]
    if not args.no_self:
        targets.append(("scivo-harness", sys.executable))

    print(f"project     {config.root}")
    before_sha, before_version = await _sha(config)
    print(f"mcp session {before_version} @ {before_sha}")
    print()

    failed = False
    moved_any = False
    for dist, interpreter in targets:
        ok, moved = await _update_one(inspect_install(interpreter, dist), args.check)
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
        print(ui.dim("  edit the REPLACE-ME values, then: scivo --provider local doctor"))
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


async def _chat(args, prompt_text: str | None = None) -> int:
    config = load()
    session = await build(
        config,
        profile=args.profile,
        read_only=args.read_only,
        provider=args.provider,
        model=args.model,
        effort=args.effort,
        with_guide=not args.no_guide,
        resume=args.resume,
        max_budget_usd=args.budget,
    )
    if prompt_text is not None:
        from claude_agent_sdk import AssistantMessage, TextBlock, query

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
    args = _parser().parse_args(argv)
    handlers = {
        "status": _status,
        "doctor": _doctor,
        "tools": _tools,
        "providers": _providers,
        "shim": _shim,
        "update": _update,
        "run": lambda a: _chat(a, " ".join(a.prompt)),
        "chat": _chat,
        None: _chat,
    }
    try:
        return asyncio.run(handlers[args.command](args))
    except (ConfigError, ProjectMismatch, ProviderError) as exc:
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
