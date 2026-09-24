"""Whether this harness still fits the MCP it is talking to.

The MCP moves almost daily. Pinning it would put this package on a treadmill it
would eventually stop running on, and freezing a research tool's server to a
months-old commit is its own bug. So the harness does not try to freeze the
server — it names the parts it depends on and reports when one is gone.

The failure this prevents: a tool preflight calls disappears upstream, every
call still "succeeds" at the transport level, the briefing quietly loses a
section, and the session behaves as though the project has no papers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Every scivo tool this harness calls by name. Preflight tolerates any one of
# them failing — it records the error and carries on — which is right at run
# time and useless as a warning, because nobody reads a briefing looking for
# what is missing from it.
# name -> (why we call it, the arguments we pass)
REQUIRED: dict[str, tuple[str, tuple[str, ...]]] = {
    "whoami": ("verifies the key is bound to this project", ()),
    "project_guide": ("the conventions, carried in the system prompt", ()),
    "get_project_memory": ("standing context", ()),
    "get_project_skills": ("the project's own playbook", ()),
    "list_decisions": ("what has already been settled", ()),
    "list_papers": ("the papers the briefing is built from", ("summary",)),
    "count_open_user_comments": ("open human comments per paper", ("slug",)),
    "review_triage_summary": ("AI findings and unrebutted rejections", ("slug",)),
    "check_requirements": ("journal limit violations", ("slug",)),
    "list_verification_findings": ("unacknowledged citation problems", ("slug",)),
    "list_servers": ("the compute registry", ()),
    "list_datasets": ("where the data is", ()),
    "list_todos": ("open work", ()),
}


def schema_of(tool: object) -> dict:
    """mcp 1.x calls it inputSchema, 2.x input_schema. Either may be in play."""
    for attribute in ("input_schema", "inputSchema"):
        schema = getattr(tool, attribute, None)
        if isinstance(schema, dict):
            return schema
    return {}


@dataclass
class Compatibility:
    present: list[str] = field(default_factory=list)
    missing: dict[str, str] = field(default_factory=dict)
    drifted: dict[str, str] = field(default_factory=dict)
    total_tools: int = 0

    @property
    def ok(self) -> bool:
        return not self.missing and not self.drifted

    def report(self) -> str:
        if self.ok:
            return f"{len(self.present)}/{len(REQUIRED)} required tools present, arguments match"
        lines: list[str] = []
        if self.missing:
            lines.append(f"{len(self.missing)} required tool(s) missing from this MCP:")
            lines += [f"  {name} — {why}" for name, why in sorted(self.missing.items())]
        if self.drifted:
            lines.append(f"{len(self.drifted)} tool(s) no longer take what we pass:")
            lines += [f"  {name} — {detail}" for name, detail in sorted(self.drifted.items())]
        lines.append("The MCP moved and this harness has not caught up. "
                     "`scivo update`, and report it if that does not fix it.")
        return "\n".join(lines)


def check(tools: list) -> Compatibility:
    """Existence AND arguments.

    Existence alone is the easy half. A tool that kept its name and renamed an
    argument fails at call time, and preflight — which tolerates any single call
    failing — turns that into a missing briefing section rather than an error.
    So compare both directions: every argument we pass must still be accepted,
    and any argument newly made required must be one we pass.
    """
    by_name = {getattr(t, "name", ""): t for t in tools}
    missing, drifted, present = {}, {}, []

    for name, (why, passes) in REQUIRED.items():
        tool = by_name.get(name)
        if tool is None:
            missing[name] = why
            continue
        present.append(name)
        schema = schema_of(tool)
        accepted = set(schema.get("properties") or {})
        required_args = set(schema.get("required") or [])

        unknown = [a for a in passes if a not in accepted]
        unmet = sorted(required_args - set(passes))
        problems = []
        if unknown:
            problems.append(f"we pass {', '.join(unknown)}, which it no longer accepts")
        if unmet:
            problems.append(f"it now requires {', '.join(unmet)}, which we do not pass")
        if problems:
            drifted[name] = "; ".join(problems)

    return Compatibility(present=sorted(present), missing=missing, drifted=drifted,
                         total_tools=len(by_name))


def shadowing_binaries() -> list[str]:
    """Every `scivo` on PATH, when there is more than one.

    Found the hard way: a second copy installed into another environment took
    precedence and ran stale code for an hour, with every symptom pointing at
    the code that was being edited. The environments were both this package —
    which is exactly why nothing looked wrong.
    """
    import os

    seen: list[str] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, "scivo")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            resolved = os.path.realpath(candidate)
            if resolved not in seen:
                seen.append(resolved)
    return seen if len(seen) > 1 else []
