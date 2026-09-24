"""Stub the Claude Agent SDK so the package imports where the SDK is absent.

Any attribute asked of `claude_agent_sdk` or `claude_agent_sdk.types` is a
throwaway class with that name; the two permission results keep the fields
tests read. Installed before any test module imports the package."""
from __future__ import annotations

import sys
import types


def _module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = []  # type: ignore[attr-defined]  # a package, so submodules resolve

    def __getattr__(attr: str):
        if attr.startswith("__"):
            raise AttributeError(attr)
        return type(attr, (), {"__init__": lambda self, *a, **k: None})

    mod.__getattr__ = __getattr__  # type: ignore[attr-defined]
    return mod


if "claude_agent_sdk" not in sys.modules or not hasattr(sys.modules["claude_agent_sdk"], "__path__"):
    sdk = _module("claude_agent_sdk")
    sdk_types = _module("claude_agent_sdk.types")

    class PermissionResultAllow:
        def __init__(self, updated_input=None, **kw):
            self.updated_input = updated_input

    class PermissionResultDeny:
        def __init__(self, message="", **kw):
            self.message = message

    sdk_types.PermissionResultAllow = PermissionResultAllow  # type: ignore[attr-defined]
    sdk_types.PermissionResultDeny = PermissionResultDeny  # type: ignore[attr-defined]
    sdk.types = sdk_types  # type: ignore[attr-defined]
    sdk.HookMatcher = lambda *a, **k: None  # type: ignore[attr-defined]
    sys.modules["claude_agent_sdk"] = sdk
    sys.modules["claude_agent_sdk.types"] = sdk_types
