"""Four small things from the 2026-11-10 review, each with the failure it had."""
from __future__ import annotations

import asyncio
import time

from scivo_harness import control as C
from scivo_harness import guardrails, prompt, providers, toolsets
from scivo_harness.config import ScivoConfig
from scivo_harness.preflight import Briefing
from scivo_harness.scivo_mcp import ToolOutcome


# ── providers.toml is found from wherever in the project you stand ──────────

def test_a_subdirectory_finds_the_projects_providers(tmp_path, monkeypatch):
    """The project's CHOICE of provider was read from the root and the
    provider it names from the current directory: "unknown provider 'local'"
    one directory down."""
    (tmp_path / ".mcp.json").write_text("{}")
    (tmp_path / ".scivo").mkdir()
    (tmp_path / ".scivo" / "providers.toml").write_text('[local]\nbase_url = "http://x"\nmodel = "q"\n')
    (tmp_path / "analysis" / "deep").mkdir(parents=True)
    monkeypatch.delenv("SCIVO_PROVIDERS", raising=False)
    monkeypatch.chdir(tmp_path / "analysis" / "deep")
    assert "local" in providers.load_all()
    assert providers.get("local").base_url == "http://x"


# ── one definition of "a read" ──────────────────────────────────────────────

def test_the_breaker_and_the_auto_approval_agree_on_what_a_read_is():
    names = ["get_section", "list_papers", "diff_submission", "validate_references",
             "server_status", "review_triage_summary", "update_section", "add_figure",
             "submit_remote_job", "whoami", "wait_for_run"]
    for name in names:
        assert guardrails.is_scivo_write("mcp__scivo__" + name) is (not toolsets.is_read_only(name)), name
    assert not hasattr(guardrails, "READ_PREFIXES"), "a second list is how they drifted"


def test_a_read_that_one_list_lacked_no_longer_resets_the_loop_counter():
    """`validate_references` counted as a write here, so a loop with it in the
    middle wiped its own count every time round and was never stopped."""
    rails = guardrails.Guardrails()

    def call(tool, **args):
        out = asyncio.run(rails.on_any_tool(
            {"tool_name": "mcp__scivo__" + tool, "tool_input": args}, None, None))
        return (out.get("hookSpecificOutput") or {}).get("permissionDecision")

    decisions = []
    for _ in range(4):
        decisions.append(call("list_papers"))
        call("validate_references", slug="p")
    assert "deny" in decisions


# ── the charter says what the harness does ──────────────────────────────────

def test_the_charter_matches_the_guardrails():
    text = prompt.build(Briefing(identity={"project_id": "p"}), toolsets.plan(["whoami"]), guide=None)
    # Hardware in memory is refused, not held: the model was told it would be
    # put to the user, and wrote as if an approval were coming.
    assert "is **blocked**" in text
    assert "hardware" in text and "held for the user's approval**. Machines" not in text
    # …and the thing that IS held is named.
    assert "outside the project" in text and "held for the user's" in text


# ── a device whose clock is behind does not lose its message ────────────────

class Inbox:
    def __init__(self, docs):
        self.docs, self.asked = docs, []

    async def call(self, name, **kw):
        if name == "list_responses":
            since = kw.get("since_seq")
            self.asked.append(since)
            rows = [{"id": k, **v} for k, v in self.docs.items()
                    if since is None or v.get("seq", 0) > since]
            return ToolOutcome(True, items=rows)
        return ToolOutcome(True, items=[{}])


def test_a_message_stamped_earlier_than_one_already_taken_in_still_arrives(tmp_path, monkeypatch):
    desktop, phone = 1_731_229_260_000, 1_731_229_200_000        # the phone is a minute behind
    docs = {"inbox-d": {"doc": "inbox", "id": "inbox-d", "seq": desktop, "text": "from the desktop",
                        "reviewer": "owner", "sid": "s"}}
    monkeypatch.setattr(C, "POLL_LADDER", ((0.0, 0.02),))

    async def main():
        ctl = C.Control(ScivoConfig(root=tmp_path, command="x"), "p", "pid", "m")
        ctl._client = Inbox(docs)
        ctl.link, ctl.sid = C.Link("pub", "url"), "s"
        ctl._listen_supported = False                             # an MCP without the listener
        ctl._last_activity = time.monotonic()
        task = asyncio.create_task(ctl._poller())
        first = await asyncio.wait_for(ctl.messages.get(), timeout=2)
        docs["inbox-p"] = {"doc": "inbox", "id": "inbox-p", "seq": phone, "text": "from the phone",
                           "reviewer": "owner", "sid": "s"}
        second = await asyncio.wait_for(ctl.messages.get(), timeout=2)
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return ctl, first, second
    ctl, first, second = asyncio.run(main())
    assert (first, second) == ("from the desktop", "from the phone")
    # Asked from below the highest seq — and nothing was delivered twice.
    assert ctl._client.asked[-1] == desktop - C.SEQ_SKEW_MS
    assert ctl.messages.empty()
