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
REQUIRED = {
    "whoami": "verifies the key is bound to this project",
    "project_guide": "the conventions, carried in the system prompt",
    "get_project_memory": "standing context",
    "get_project_skills": "the project's own playbook",
    "list_decisions": "what has already been settled",
    "list_papers": "the papers the briefing is built from",
    "count_open_user_comments": "open human comments per paper",
    "review_triage_summary": "AI findings and unrebutted rejections",
    "check_requirements": "journal limit violations",
    "list_verification_findings": "unacknowledged citation problems",
    "list_servers": "the compute registry",
    "list_datasets": "where the data is",
    "list_todos": "open work",
}


@dataclass
class Compatibility:
    present: list[str] = field(default_factory=list)
    missing: dict[str, str] = field(default_factory=dict)
    total_tools: int = 0

    @property
    def ok(self) -> bool:
        return not self.missing

    def report(self) -> str:
        if self.ok:
            return f"{len(self.present)}/{len(REQUIRED)} required tools present"
        lines = [f"{len(self.missing)} required tool(s) missing from this MCP:"]
        lines += [f"  {name} — {why}" for name, why in sorted(self.missing.items())]
        lines.append("The MCP moved and this harness has not caught up. "
                     "`scivo update`, and report it if that does not fix it.")
        return "\n".join(lines)


def check(tool_names: list[str]) -> Compatibility:
    available = set(tool_names)
    return Compatibility(
        present=sorted(REQUIRED.keys() & available),
        missing={name: why for name, why in REQUIRED.items() if name not in available},
        total_tools=len(available),
    )
