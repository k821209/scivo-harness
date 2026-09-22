"""Tool profiles — which of the server's 234 tools this session carries.

Measured on this project: the full scivo tool surface is ~174 KB of name,
description and JSON schema, about 43,600 tokens, on the prefix of every
request. It caches, but it is still the largest single thing in the prompt and
every tool in it is a tool the model can pick wrongly. A manuscript session has
no use for the YouTube uploader.

So the harness prunes by domain. `disallowed_tools` with a bare tool name
removes it from context entirely (a scoped rule like `Bash(rm *)` only denies
the call), which is the lever that makes this a context saving and not just a
permission.
"""

from __future__ import annotations

from dataclasses import dataclass

PREFIX = "mcp__scivo__"

# Domains, matched against the bare tool name by substring. A tool matching no
# domain falls into `core`, so a tool added upstream is carried, never dropped
# silently — the failure mode of a deny-list is much kinder than a miss here.
DOMAINS: dict[str, tuple[str, ...]] = {
    "paper": (
        "paper", "section", "figure", "table", "reference", "review", "citation",
        "manuscript", "export", "submission", "requirement", "journal", "author",
        "affiliation", "supplementary", "doi", "works",
        "finding", "anchor", "legend", "image", "publication", "passcode",
        "publish_page", "page_data", "stud",
    ),
    # A domain of its own: uploading a reference image, a clip, a briefing
    # document is what materials are for, so a video or a deck session needs
    # them as much as a paper session does, and a paper session with no
    # materials tools was a paper session that could not attach anything.
    "materials": ("material", "asset"),
    "analysis": (
        "analysis", "analyses", "run", "server", "dataset", "pipeline", "job",
        "workdir", "remote", "local_job", "log", "output", "graph", "plan",
    ),
    "deck": ("deck", "slide", "pptx", "region", "renumber", "reorder_deck"),
    "video": ("video", "youtube"),
}

ALWAYS = (
    "whoami", "project_guide", "get_project_memory", "append_project_memory",
    "update_project_memory", "get_project_skills", "update_project_skills",
    "list_todos", "add_todo", "update_todo", "list_activity", "log_activity",
    "list_decisions", "record_decision", "report_feedback", "list_feedback",
    "get_plan", "list_my_projects", "search_my_papers", "read_project_paper",
    "read_project_memory", "list_project_materials", "get_project_material",
    "list_project_papers", "get_user_secret", "list_user_secrets",
)

READ_ONLY_PREFIXES = (
    "list_", "get_", "read_", "search_", "count_", "check_", "verify_",
    "preview_", "diff_", "compare_", "lint_", "validate_", "scan_", "poll_",
    "tail_", "whoami", "project_guide", "server_status", "review_triage",
)

PROFILES: dict[str, tuple[str, ...]] = {
    "full": ("paper", "materials", "analysis", "deck", "video"),
    "paper": ("paper", "materials", "analysis"),
    "writing": ("paper", "materials"),
    "analysis": ("analysis",),
    # Decks and videos both attach materials, so those profiles carry them —
    # and no longer drag along the paper tools you do not need to make a deck
    # or upload a video.
    "deck": ("deck", "materials"),
    "video": ("video", "materials"),
}


@dataclass
class ToolPlan:
    kept: list[str]
    dropped: list[str]
    read_only: list[str]
    profile: str
    read_only_session: bool

    @property
    def disallowed(self) -> list[str]:
        return [PREFIX + name for name in self.dropped]

    @property
    def auto_approved(self) -> list[str]:
        """Read-only scivo tools never need a prompt; asking teaches nothing."""
        return [PREFIX + name for name in self.read_only]

    def summary(self) -> str:
        total = len(self.kept) + len(self.dropped)
        return (
            f"profile {self.profile}: {len(self.kept)}/{total} scivo tools "
            f"({len(self.dropped)} dropped from context)"
            + (", read-only session" if self.read_only_session else "")
        )


def _domain_of(name: str) -> str:
    lowered = name.lower()
    for domain, needles in DOMAINS.items():
        if any(needle in lowered for needle in needles):
            return domain
    return "core"


def is_read_only(name: str) -> bool:
    return name.startswith(READ_ONLY_PREFIXES)


def plan(all_tools: list[str], profile: str = "full", read_only: bool = False) -> ToolPlan:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; choose from {', '.join(PROFILES)}")
    wanted = set(PROFILES[profile])
    kept, dropped = [], []
    for name in all_tools:
        keep = name in ALWAYS or _domain_of(name) in wanted or _domain_of(name) == "core"
        if keep and read_only and not is_read_only(name):
            keep = False
        (kept if keep else dropped).append(name)
    return ToolPlan(
        kept=kept,
        dropped=dropped,
        read_only=[n for n in kept if is_read_only(n)],
        profile=profile,
        read_only_session=read_only,
    )
