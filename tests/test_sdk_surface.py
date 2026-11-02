"""Does the installed Agent SDK still have the surface the harness uses?

`pyproject.toml` pins `claude-agent-sdk>=0.2.153` — a floor, not a version, so
every install takes whatever is newest. On 2026-11-02 installing the harness
moved it to 0.2.176 unasked, and nothing would have said so.

The rest of this suite cannot catch that: `conftest.py` stubs the SDK with a
module whose `__getattr__` answers ANY name with a throwaway class accepting
any arguments, so a dropped export or a renamed option passes every test and
fails at the first real session. That stub is the right call for the suite —
the package must import where the SDK is absent — but it means this check has
to run OUTSIDE pytest's process, against the SDK that is really installed.

So the checks shell out to this same file run as a script. It reads the
harness's own source for what to verify, rather than a list someone must
remember to update: every name imported from `claude_agent_sdk`, and every
keyword argument handed to the SDK objects the harness constructs. No network
and no credential.
"""
from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import json
import pathlib
import subprocess
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "scivo_harness"

# The SDK objects the harness constructs. A kwarg they do not accept is a
# TypeError in a real session, which is why the check is by signature.
CONSTRUCTED = ("ClaudeAgentOptions", "ClaudeSDKClient", "HookMatcher", "query",
               "PermissionResultAllow", "PermissionResultDeny")


def _sources() -> list[ast.Module]:
    return [ast.parse(f.read_text(encoding="utf-8")) for f in sorted(SRC.rglob("*.py"))]


def _imported() -> dict[str, list[str]]:
    """{module: names} for every `from claude_agent_sdk… import …` in the harness."""
    out: dict[str, set[str]] = {}
    for tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("claude_agent_sdk"):
                out.setdefault(node.module, set()).update(a.name for a in node.names)
    return {mod: sorted(names) for mod, names in out.items()}


def _kwargs_passed() -> dict[str, list[str]]:
    out: dict[str, set[str]] = {name: set() for name in CONSTRUCTED}
    for tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name in out:
                out[name].update(kw.arg for kw in node.keywords if kw.arg)
    return {name: sorted(names) for name, names in out.items()}


def _accepted(name: str) -> set[str]:
    sdk = importlib.import_module("claude_agent_sdk")
    obj = getattr(sdk, name, None)
    if obj is None:
        obj = getattr(importlib.import_module("claude_agent_sdk.types"), name)
    if dataclasses.is_dataclass(obj):
        return {f.name for f in dataclasses.fields(obj)}
    return set(inspect.signature(obj).parameters)


def inspect_installed() -> dict:
    """The verdict, computed in a process where the SDK is the real one."""
    import claude_agent_sdk  # noqa: F401  — absent means nothing to check

    missing: list[str] = []
    for module, names in _imported().items():
        mod = importlib.import_module(module)
        missing += [f"{module}.{n}" for n in names if not hasattr(mod, n)]

    rejected: dict[str, list[str]] = {}
    for name, passed in _kwargs_passed().items():
        if not passed:
            continue
        gone = sorted(set(passed) - _accepted(name))
        if gone:
            rejected[name] = gone

    from importlib.metadata import version
    return {"version": version("claude-agent-sdk"), "imports": _imported(),
            "missing": missing, "rejected": rejected}


def _verdict() -> dict:
    """Run the inspection in a clean interpreter, past conftest's stub."""
    done = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve())],
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        return {"unavailable": done.stderr.strip()[-400:]}
    return json.loads(done.stdout)


def test_the_harness_imports_nothing_the_installed_sdk_dropped():
    verdict = _verdict()
    if "unavailable" in verdict:
        import pytest
        pytest.skip(f"no SDK to check here: {verdict['unavailable']}")
    assert verdict["imports"], "no SDK imports found — the walk looked in the wrong place"
    assert not verdict["missing"], (
        f"gone from claude-agent-sdk {verdict['version']}: {verdict['missing']}")


def test_every_kwarg_the_harness_passes_is_still_accepted():
    verdict = _verdict()
    if "unavailable" in verdict:
        import pytest
        pytest.skip(f"no SDK to check here: {verdict['unavailable']}")
    assert not verdict["rejected"], (
        f"claude-agent-sdk {verdict['version']} no longer accepts: {verdict['rejected']}")


if __name__ == "__main__":
    print(json.dumps(inspect_installed()))
