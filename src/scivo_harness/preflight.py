"""The session-start protocol, executed before the first token.

`project_guide` states the protocol as a numbered list and then says, more than
once, that it is the step most often skipped. It is skipped because it is a
request to a model rather than a property of the session. Here it is neither
optional nor a judgement: the harness calls the tools itself, and the model
starts the conversation already holding the answers.

Nothing here costs a model token — `scivo status` runs the same code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .config import ScivoConfig
from .scivo_mcp import ScivoClient, ToolOutcome

MEMORY_BUDGET = 6000  # chars; memory is a curated digest, not a log
NOTE_BUDGET = 400


class ProjectMismatch(RuntimeError):
    """`.mcp.json` and `CLAUDE.md` came from two different dashboard projects."""


@dataclass
class PaperBrief:
    slug: str
    title: str = ""
    status: str = ""
    journal: str = ""
    open_comments: int = 0
    triage: dict[str, Any] = field(default_factory=dict)
    requirement_violations: list[Any] = field(default_factory=list)
    requirements_configured: bool = False
    findings: list[Any] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.open_comments
            or self.requirement_violations
            or self.findings
            or self.triage.get("ai_open")
            or self.triage.get("rejected_without_rationale")
        )


@dataclass
class Briefing:
    identity: dict[str, Any] = field(default_factory=dict)
    memory: str = ""
    skills: str = ""
    decisions: list[Any] = field(default_factory=list)
    papers: list[PaperBrief] = field(default_factory=list)
    servers: list[Any] = field(default_factory=list)
    datasets: list[Any] = field(default_factory=list)
    todos: list[Any] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def project_id(self) -> str:
        return str(self.identity.get("project_id", "?"))

    @property
    def project_name(self) -> str:
        return str(self.identity.get("project_name") or self.project_id)

    @property
    def attention(self) -> list[PaperBrief]:
        return [p for p in self.papers if p.needs_attention]


def _clip(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit].rstrip() + f" …[{len(text)} chars total]"


def _doc_content(outcome: ToolOutcome) -> str:
    """get_project_memory / get_project_skills return {content, updated_at, ...}."""
    value = outcome.first
    if isinstance(value, dict):
        return str(value.get("content") or "")
    return str(value or "")


async def gather(client: ScivoClient, config: ScivoConfig) -> Briefing:
    brief = Briefing()

    who = await client.call("whoami")
    if not who:
        raise ProjectMismatch(f"whoami failed — the MCP server did not start: {who.error}")
    brief.identity = who.first or {}

    expected = config.expected_project_id
    actual = brief.project_id
    if expected and expected != actual:
        raise ProjectMismatch(
            f"CLAUDE.md names project {expected} but {config.source} is bound to {actual}.\n"
            "The two files were taken from different dashboard projects. "
            "Re-download both from the same project's Setup tab."
        )

    # Project-wide context: one round trip each, all independent.
    memory, skills, decisions, servers, datasets, todos, papers = await asyncio.gather(
        client.call("get_project_memory"),
        client.call("get_project_skills"),
        client.call("list_decisions"),
        client.call("list_servers"),
        client.call("list_datasets"),
        client.call("list_todos"),
        client.call("list_papers", summary=True),
    )
    brief.memory = _doc_content(memory)
    brief.skills = _doc_content(skills)
    brief.decisions = decisions.items
    brief.servers = servers.items
    brief.datasets = datasets.items
    brief.todos = [t for t in todos.items if str(_get(t, "status", "")) != "done"]

    for name, outcome in [
        ("get_project_memory", memory), ("get_project_skills", skills),
        ("list_decisions", decisions), ("list_servers", servers),
        ("list_datasets", datasets), ("list_todos", todos), ("list_papers", papers),
    ]:
        if not outcome:
            brief.errors.append(f"{name}: {outcome.error}")

    brief.papers = await asyncio.gather(*(_paper(client, p) for p in papers.items))
    for paper in brief.papers:
        brief.errors.extend(getattr(paper, "_errors", []))
    return brief


def _get(record: Any, key: str, default: Any = None) -> Any:
    return record.get(key, default) if isinstance(record, dict) else default


async def _paper(client: ScivoClient, record: Any) -> PaperBrief:
    slug = str(_get(record, "slug", record))
    paper = PaperBrief(
        slug=slug,
        title=str(_get(record, "title", "") or ""),
        status=str(_get(record, "status", "") or ""),
        journal=str(_get(record, "journal", "") or ""),
    )
    comments, triage, reqs, findings = await asyncio.gather(
        client.call("count_open_user_comments", slug=slug),
        client.call("review_triage_summary", slug=slug),
        client.call("check_requirements", slug=slug),
        client.call("list_verification_findings", slug=slug),
    )
    errors = []
    if comments:
        value = comments.first
        paper.open_comments = value if isinstance(value, int) else _get(value, "open", 0) or 0
    else:
        errors.append(f"count_open_user_comments({slug}): {comments.error}")
    if triage:
        paper.triage = triage.first if isinstance(triage.first, dict) else {}
    if reqs:
        value = reqs.first if isinstance(reqs.first, dict) else {}
        paper.requirements_configured = bool(value.get("configured"))
        paper.requirement_violations = value.get("violations") or []
    paper.findings = findings.items if findings else []
    paper._errors = errors  # type: ignore[attr-defined]
    return paper


# ---------------------------------------------------------------- rendering


def to_markdown(brief: Briefing) -> str:
    """The briefing as it is appended to the system prompt."""
    out: list[str] = [
        "# Session briefing (gathered by the harness before this conversation started)",
        "",
        "These are the session-start calls from the project guide, already made. "
        "Do not repeat them; call a tool again only when you need a value this "
        "briefing does not carry, or after something you did changed it.",
        "",
        f"**Project** `{brief.project_name}` · id `{brief.project_id}` · "
        f"guide {brief.identity.get('guide_version', '?')}",
    ]
    if brief.identity.get("update_available"):
        out.append(
            f"**The MCP install is behind the latest build** "
            f"({brief.identity.get('installed_version')} → {brief.identity.get('latest_version')}). "
            "Tell the user before relying on tool behavior."
        )

    out += ["", "## Project memory", ""]
    out.append(_clip(brief.memory, MEMORY_BUDGET) if brief.memory.strip()
               else "_empty — nothing durable recorded yet._")

    if brief.skills.strip():
        out += ["", "## Project playbook (Memory tab, follow for this project)", "",
                _clip(brief.skills, MEMORY_BUDGET)]

    out += ["", "## Decisions already settled", ""]
    if brief.decisions:
        for decision in brief.decisions[:12]:
            text = _get(decision, "text", decision)
            why = _get(decision, "rationale")
            line = f"- {text}"
            if why:
                line += f" — _{_clip(why, NOTE_BUDGET)}_"
            if _get(decision, "superseded_by"):
                line += " **(superseded)**"
            out.append(line)
        out.append("")
        out.append("Re-opening one of these is the most expensive thing you can do here.")
    else:
        out.append("_none recorded._")

    out += ["", "## Papers", ""]
    if brief.papers:
        for paper in brief.papers:
            bits = [f"**`{paper.slug}`**"]
            if paper.title:
                bits.append(paper.title)
            meta = " · ".join(x for x in (paper.status, paper.journal) if x)
            if meta:
                bits.append(f"({meta})")
            out.append("- " + " ".join(bits))
            if paper.open_comments:
                out.append(f"  - **{paper.open_comments} open human comment(s)** — offer `/paper-revision`.")
            ai_open = paper.triage.get("ai_open")
            if ai_open:
                out.append(f"  - {ai_open} open AI review finding(s) — drive to 0 before calling a review handled.")
            unrebutted = paper.triage.get("rejected_without_rationale")
            if unrebutted:
                out.append(f"  - {unrebutted} rejected comment(s) with no rationale — these block a clean export.")
            if paper.requirement_violations:
                out.append(f"  - journal requirement violations: {paper.requirement_violations}")
            elif not paper.requirements_configured and paper.journal:
                out.append(f"  - no requirements captured for {paper.journal} — suggest `/journal-requirements`.")
            if paper.findings:
                out.append(f"  - {len(paper.findings)} unacknowledged citation finding(s) — surface and fix them.")
    else:
        out.append("_no papers yet._")

    out += ["", "## Compute registry (the Runs tab's inventory — NOT project memory)", ""]
    if brief.servers:
        for server in brief.servers:
            alias = _get(server, "alias", "?")
            gpus = _get(server, "gpus", 0) or 0
            line = (f"- `{alias}` {_get(server, 'host', '?')} · {_get(server, 'cores', '?')} cores"
                    f" · {_get(server, 'memory_gb') or '?'} GB · {gpus} GPU")
            workdir = _get(server, "default_workdir")
            if workdir:
                line += f" · workdir `{workdir}`"
            out.append(line)
            note = _get(server, "notes")
            if note:
                out.append(f"  - {_clip(note, NOTE_BUDGET)}")
    else:
        out.append("_no servers registered._")

    out += ["", "## Datasets", ""]
    if brief.datasets:
        for dataset in brief.datasets:
            out.append(
                f"- `{_get(dataset, 'name', '?')}` on {_get(dataset, 'server_alias') or 'local'} "
                f"at `{_get(dataset, 'path', '?')}` — ids: {_get(dataset, 'id_convention') or 'unrecorded'}"
            )
    else:
        out.append("_none registered. `list_datasets` being empty is not evidence the project has no data._")

    if brief.todos:
        out += ["", "## Open to-dos", ""]
        out += [f"- [{_get(t, 'status', 'todo')}] {_get(t, 'text', t)}" for t in brief.todos[:15]]

    if brief.errors:
        out += ["", "## Preflight could not read", ""]
        out += [f"- {e}" for e in brief.errors]

    return "\n".join(out)
