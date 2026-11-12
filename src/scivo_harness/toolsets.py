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

# Every scivo tool a guardrail's refusal sends the model to, and what a job
# launched that way needs afterwards. A refusal that names a tool the session
# does not carry is a dead end with a signpost on it: "use submit_remote_job"
# was said 358 times in one turn to a `--profile video` session that had no
# such tool, for five hours (feedback d291849a7b1a). The guards do not know
# what the profile is, so every profile that loads anything loads these.
PRESCRIBED = (
    "submit_remote_job", "tail_remote_log", "poll_remote_pids", "wait_for_run",
    "kill_remote_job", "mark_run_finished", "create_analysis", "record_analysis_run",
    "list_servers", "get_server", "add_server", "update_server", "add_server_env",
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
    # No scivo surface at all — asked for as "완전 프리한걸로" (user,
    # 2026-11-08): the harness's charter, the briefing and the host's own
    # tools, and nothing of this server. An EMPTY domain list is the rule,
    # not the name: see plan().
    "none": (),
}


# The host's own tools that change files. `--read-only` promised "no writing
# tools at all" and removed only the scivo ones: Write and Edit stayed loaded,
# in a session whose prompt told the model every writing tool was absent
# (review 2026-11-10). Bash is NOT here — reading a log needs it — so in a
# read-only session it stays and is asked about every time (permissions.py).
HOST_FILE_WRITERS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


@dataclass
class ToolPlan:
    kept: list[str]
    dropped: list[str]
    read_only: list[str]
    profile: str
    read_only_session: bool
    # True when this is the kit of a LOCAL model: the profile's plan, cut down
    # to what a small model can carry (session.kit_plan). The counts are then
    # the kit's, which is what is loaded.
    lean: bool = False

    @property
    def disallowed(self) -> list[str]:
        """Everything this session must not carry. One list, read wherever
        options are built — a second place that rebuilt it (a profile switch)
        would otherwise hand the file tools back to a read-only session."""
        out = [PREFIX + name for name in self.dropped]
        if self.read_only_session:
            out += list(HOST_FILE_WRITERS)
        return out

    @property
    def auto_approved(self) -> list[str]:
        """Read-only scivo tools never need a prompt; asking teaches nothing."""
        return [PREFIX + name for name in self.read_only]

    def summary(self) -> str:
        total = len(self.kept) + len(self.dropped)
        return (
            f"profile {self.profile}: {len(self.kept)}/{total} scivo tools "
            f"({len(self.dropped)} dropped from context)"
            + (" — the kit for a local model" if self.lean else "")
            + (", read-only session" if self.read_only_session else "")
        )


# Most specific first. `paper` last: its needles are the broadest, and with
# it first `preview_slide` matched `review` and `export_deck_to_pptx` matched
# `export`, so a `--profile deck` session lost the two tools /paper-deck cannot
# work without.
_DOMAIN_ORDER = ("deck", "video", "materials", "analysis", "paper")

# A tool one domain claims that other profiles cannot work without. The
# video loop registers keyframes (first/last images) BEFORE generating a
# chunk, and those keyframes come from `generate_image` — which `image`
# files under paper, so a `--profile video` session had no way to make
# the thing its own tab asks the user to judge.
ALSO: dict[str, tuple[str, ...]] = {
    "generate_image": ("video", "deck"),
}


def _matches(needle: str, name: str, segments: list[str]) -> bool:
    if "_" in needle:                 # a multi-word needle names a tool family
        return needle in name
    return any(seg.startswith(needle) for seg in segments)


def _domain_of(name: str) -> str:
    lowered = name.lower()
    segments = lowered.split("_")
    for domain in _DOMAIN_ORDER:
        if any(_matches(needle, lowered, segments) for needle in DOMAINS[domain]):
            return domain
    return "core"


# Tools the HARNESS calls and the model has no use for. Dropped from every
# profile: `wait_for_responses` holds a call open for half a minute waiting on
# the control page, which is the control channel's own plumbing — in the
# model's hands it is a schema to carry and a way to stall a turn.
HARNESS_ONLY = ("wait_for_responses",)


# Read-only by name, but the value comes back into the transcript: a
# plaintext account token is not something to hand over without a prompt.
NEVER_AUTO = ("get_user_secret", "list_user_secrets")


def is_read_only(name: str) -> bool:
    return name.startswith(READ_ONLY_PREFIXES)


def plan(all_tools: list[str], profile: str = "full", read_only: bool = False) -> ToolPlan:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; choose from {', '.join(PROFILES)}")
    wanted = set(PROFILES[profile])
    if not wanted:
        # Nothing, and that includes ALWAYS and the `core` domain — which is
        # the whole difference between this and a narrow profile. Everything
        # below keeps core unconditionally, so an empty profile that went
        # through it would still carry whoami, memory, todos, decisions and
        # feedback: ~40 tools, which is not what "no tools" means. Derived
        # from the empty list rather than from the name, so a second empty
        # profile behaves the same way instead of silently differently.
        return ToolPlan(kept=[], dropped=list(all_tools), read_only=[],
                        profile=profile, read_only_session=read_only)
    kept, dropped = [], []
    for name in all_tools:
        keep = (name in ALWAYS or name in PRESCRIBED
                or _domain_of(name) in wanted or _domain_of(name) == "core"
                or bool(wanted & set(ALSO.get(name, ()))))
        if name in HARNESS_ONLY:
            keep = False
        if keep and read_only and not is_read_only(name):
            keep = False
        (kept if keep else dropped).append(name)
    return ToolPlan(
        kept=kept,
        dropped=dropped,
        read_only=[n for n in kept if is_read_only(n) and n not in NEVER_AUTO],
        profile=profile,
        read_only_session=read_only,
    )
