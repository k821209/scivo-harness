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
# A server this project has no working directory or dataset on gets the first
# line of its notes; one it does gets them to NOTE_BUDGET. The registry is the
# whole ACCOUNT's, and thirteen machines' notes came to 6 KB on a project that
# computes on none of them (review 2026-11-11).
NOTE_ELSEWHERE = 160
VIOLATION_BUDGET = 280
# A to-do is a line of work, and some are written as a page of findings. The
# start of it says what it is; `list_todos()` has the rest, and the cut says so.
TODO_BUDGET = 600
# How many of each list the briefing carries. A cut is always SAID, with the
# call that returns the rest: the briefing also tells the model not to repeat
# the session-start calls, so a list cut in silence is a list the model
# believes it has read whole. Thirty decisions came through as twelve with
# "re-opening one of these is the most expensive thing you can do" under
# them, and nothing to say eighteen more were settled (review 2026-11-10).
DECISIONS_SHOWN = 12
TODOS_SHOWN = 15


class ProjectMismatch(RuntimeError):
    """`.mcp.json` and `CLAUDE.md` came from two different dashboard projects."""


class ServerUnavailable(RuntimeError):
    """The MCP server did not answer at all — a bad key, an import error, a
    wrong interpreter. Not a mismatch: the re-download guidance for that
    case pointed the wrong way here."""


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
    # Open questions on study documents, split by who asked: a reader's
    # question is work waiting for this session, the agent's own is work
    # waiting for the reader. One number would have the session nag itself.
    study_questions: dict[str, Any] = field(default_factory=dict)
    # Aliases this project has a working directory on. None: the bindings
    # could not be read — which is not "none", and is rendered as not knowing.
    bound_servers: set[str] | None = None
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


def _violation(violation: Any, slug: str, said: dict[str, str]) -> str:
    """One requirement that is not met, as a sentence — once.

    `check_requirements` runs per paper, and some of its checks are about the
    PROJECT (a to-do left open for days). Those came back identical under
    every paper, and were printed as the raw record each time: one 1.2 KB
    to-do text, three times in one briefing, counting the to-do list itself.
    The second paper now points at the first. Nothing is dropped — a shape
    this does not recognise is printed as it came, clipped.
    """
    if isinstance(violation, dict) and (violation.get("message") or violation.get("label")):
        label = str(violation.get("label") or violation.get("name") or "requirement")
        message = str(violation.get("message") or "")
        key = f"{violation.get('name')}\x00{message}"
        if key in said and said[key] != slug:
            return f"{label}: the same as under `{said[key]}` (a project-wide check)."
        said.setdefault(key, slug)
        return f"{label}: {_clip(message, VIOLATION_BUDGET)}" if message else label
    return _clip(violation, VIOLATION_BUDGET)


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
        from . import scivo_mcp as _mcp
        tail = _mcp.last_stderr()
        raise ServerUnavailable(
            f"whoami failed — the MCP server did not answer: {who.error}"
            + ("\n  server stderr:\n    " + "\n    ".join(tail.splitlines()[-8:]) if tail else "")
        )
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
    (memory, skills, decisions, servers, datasets, todos, papers,
     questions, workdirs) = await asyncio.gather(
        client.call("get_project_memory"),
        client.call("get_project_skills"),
        client.call("list_decisions"),
        client.call("list_servers"),
        client.call("list_datasets"),
        client.call("list_todos"),
        client.call("list_papers", summary=True),
        # One call for the whole project. A question a reader left on a study
        # is the one kind of waiting work that lives nowhere near a paper, so
        # nothing else in this briefing would have shown it.
        client.call("count_open_study_questions"),
        # Which of the account's servers THIS project computes on.
        client.call("list_project_workdirs"),
    )
    brief.memory = _doc_content(memory)
    brief.skills = _doc_content(skills)
    brief.decisions = decisions.items
    brief.servers = servers.items
    brief.datasets = datasets.items
    brief.todos = [t for t in todos.items if str(_get(t, "status", "")) != "done"]
    if workdirs:
        brief.bound_servers = {str(_get(w, "server_alias")) for w in workdirs.items
                               if _get(w, "server_alias")}
    else:
        # Not fatal, and not "no bindings": every note is then shown in full.
        brief.errors.append(f"list_project_workdirs: {workdirs.error}")
    counts = questions.first if questions else None
    brief.study_questions = counts if isinstance(counts, dict) else {}

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
    # Each failure is named: an empty triage, "no requirements captured" or
    # no findings used to be indistinguishable from a clean paper.
    if triage:
        paper.triage = triage.first if isinstance(triage.first, dict) else {}
    else:
        errors.append(f"review_triage_summary({slug}): {triage.error}")
    if reqs:
        value = reqs.first if isinstance(reqs.first, dict) else {}
        paper.requirements_configured = bool(value.get("configured"))
        paper.requirement_violations = value.get("violations") or []
    else:
        errors.append(f"check_requirements({slug}): {reqs.error}")
    if findings:
        paper.findings = findings.items
    else:
        paper.findings = []
        errors.append(f"list_verification_findings({slug}): {findings.error}")
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
    if brief.identity.get("install_warning"):
        out.append(f"**Install warning from the MCP — tell the user before anything else:** "
                   f"{brief.identity['install_warning']}")
    if brief.identity.get("stale_process"):
        out.append(
            "**Stale process — tell the user before anything else:** the MCP this session calls "
            "is not the code on disk"
            + (f" ({brief.identity['stale_hint']})" if brief.identity.get("stale_hint") else "")
            + ". Only a restart clears it; until then an empty lint is not a pass.")
    if brief.identity.get("install_note"):
        out.append(f"_Install note from the MCP (no action needed): {brief.identity['install_note']}_")
    if brief.identity.get("key_scope_note"):
        # Written by the server so a session that finds the key in a file
        # about to be committed does not reason "just the project key".
        out.append(f"**API key scope:** {brief.identity['key_scope_note']}")
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
        for decision in brief.decisions[:DECISIONS_SHOWN]:
            text = _get(decision, "text", decision)
            why = _get(decision, "rationale")
            line = f"- {text}"
            if why:
                line += f" — _{_clip(why, NOTE_BUDGET)}_"
            if _get(decision, "superseded_by"):
                line += " **(superseded)**"
            out.append(line)
        older = len(brief.decisions) - DECISIONS_SHOWN
        if older > 0:
            out.append(
                f"- **…and {older} older decision(s) NOT shown here.** These are the "
                f"{DECISIONS_SHOWN} most recent only — call `list_decisions()` before "
                "proposing anything they do not cover; this is the one session-start "
                "call the briefing does not replace.")
        out.append("")
        out.append("Re-opening one of these is the most expensive thing you can do here.")
    else:
        out.append("_none recorded._")

    out += ["", "## Papers", ""]
    said: dict[str, str] = {}    # a violation already written out, and under which paper
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
            for violation in paper.requirement_violations:
                out.append("  - requirement not met — " + _violation(violation, paper.slug, said))
            if paper.requirement_violations:
                pass   # said above, one line each
            elif not paper.requirements_configured and paper.journal:
                out.append(f"  - no requirements captured for {paper.journal} — suggest `/journal-requirements`.")
            if paper.findings:
                out.append(f"  - {len(paper.findings)} unacknowledged citation finding(s) — surface and fix them.")
    else:
        out.append("_no papers yet._")

    # Only when there is something waiting. A heading that says "0 questions"
    # on every project trains the reader past the one session it matters.
    q = brief.study_questions or {}
    if q.get("from_reader") or q.get("from_agent"):
        out += ["", "## Questions on study documents", ""]
        if q.get("from_reader"):
            out.append(
                f"- **{q['from_reader']} question(s) a reader left on a study** "
                f"— `list_study_questions()`, then `answer_study_question(...)`. "
                f"An answer does not have to change the document; say which with "
                f"`changed_study`.")
        if q.get("from_agent"):
            out.append(
                f"- {q['from_agent']} question(s) a PAST session left for the "
                f"reader — surface them, do not answer your own.")

    out += ["", "## Compute registry (the Runs tab's inventory — NOT project memory)", ""]
    if brief.servers:
        here = brief.bound_servers
        if here is not None:
            here = here | {str(_get(d, "server_alias")) for d in brief.datasets if _get(d, "server_alias")}
            elsewhere = sum(1 for s in brief.servers if _get(s, "alias") not in here)
            if elsewhere:
                out.append(
                    f"_The registry is the whole account's. This project has a working directory or "
                    f"a dataset on {len(brief.servers) - elsewhere} of these {len(brief.servers)}; "
                    f"the other {elsewhere} show the first line of their notes. Before using one of "
                    f"those, read `get_server(alias)` — the notes hold measured facts (ports, "
                    f"egress, shared-use rules)._")
                out.append("")
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
            if note and (here is None or alias in here):
                out.append(f"  - {_clip(note, NOTE_BUDGET)}")
            elif note:
                first = str(note).strip().split("\n", 1)[0]
                short = _clip(first, NOTE_ELSEWHERE)
                if len(short) < len(str(note).strip()) and "chars total" not in short:
                    short += f" …[{len(str(note))} chars total]"
                out.append(f"  - {short}")
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
        for t in brief.todos[:TODOS_SHOWN]:
            text = str(_get(t, "text", t))
            shown = text if len(text) <= TODO_BUDGET else (
                text[:TODO_BUDGET].rstrip() + f" …[{len(text)} chars — `list_todos()` for the rest]")
            out.append(f"- [{_get(t, 'status', 'todo')}] {shown}")
        more = len(brief.todos) - TODOS_SHOWN
        if more > 0:
            out.append(f"- …and {more} more open to-do(s) not shown — `list_todos()`.")

    if brief.errors:
        out += ["", "## Preflight could not read", ""]
        out += [f"- {e}" for e in brief.errors]

    return "\n".join(out)
