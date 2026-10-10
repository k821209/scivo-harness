"""The repeat-call breaker, and the one thing it must not break.

The first pytest in this repo (the build was exercised with one-off scratch
scripts; see README "Testing"). `claude_agent_sdk` is stubbed so the rule can
be tested where the SDK is not installed.
"""
from __future__ import annotations

import asyncio

import pytest


from scivo_harness import guardrails  # noqa: E402


def _call(g, name, args):
    return asyncio.run(g.on_any_tool({"tool_name": name, "tool_input": args}, None, None))


def test_the_same_read_three_times_in_a_turn_is_blocked():
    g = guardrails.Guardrails()
    a = {"slug": "p", "deck_id": "d", "slide_id": "s1"}
    assert _call(g, "mcp__scivo__preview_slide", a) == {}
    assert _call(g, "mcp__scivo__preview_slide", a) == {}
    out = _call(g, "mcp__scivo__preview_slide", a)
    assert out and "Blocked" in str(out)


def test_a_write_in_between_lets_the_same_read_run_again():
    """update_slide → preview_slide → update_slide → preview_slide, the loop
    /paper-deck requires: each preview follows a write, so none is a repeat."""
    g = guardrails.Guardrails()
    a = {"slug": "p", "deck_id": "d", "slide_id": "s1"}
    for i in range(6):
        assert _call(g, "mcp__scivo__update_slide", {**a, "code": f"v{i}"}) == {}
        assert _call(g, "mcp__scivo__preview_slide", a) == {}, f"blocked on round {i}"


def test_the_same_write_repeated_verbatim_is_still_a_loop():
    g = guardrails.Guardrails()
    a = {"slug": "p", "deck_id": "d", "slide_id": "s1", "code": "same"}
    assert _call(g, "mcp__scivo__update_slide", a) == {}
    assert _call(g, "mcp__scivo__update_slide", a) == {}
    assert "Blocked" in str(_call(g, "mcp__scivo__update_slide", a))


def test_read_prefixes_cover_the_read_only_surface():
    for name in ("mcp__scivo__get_slide", "mcp__scivo__list_papers", "mcp__scivo__preview_slide",
                 "mcp__scivo__lint_manuscript", "mcp__scivo__whoami", "mcp__scivo__project_guide",
                 "mcp__scivo__verify_doi", "mcp__scivo__compare_run_params"):
        assert not guardrails.is_scivo_write(name), name
    for name in ("mcp__scivo__update_slide", "mcp__scivo__add_table", "mcp__scivo__record_decision",
                 "mcp__scivo__export_deck_to_pptx", "mcp__scivo__render_slide"):
        assert guardrails.is_scivo_write(name), name
    assert not guardrails.is_scivo_write("Bash")


# ── the shell rules, against the shapes that evaded them ─────────────────────

def _bash(g, command):
    return asyncio.run(g.on_bash({"tool_name": "Bash", "tool_input": {"command": command}}, None, None))


@pytest.mark.parametrize("command", [
    'ssh gpu1 "cd /data; nohup python train.py &"',
    'ssh gpu1 "python train.py > log 2>&1 &"',
    'ssh gpu1 "echo x | nohup python train.py"',
    'ssh gpu1 "tmux new -d -s job python train.py"',
    'ssh gpu1 "screen -dmS job python train.py"',
    'ssh gpu1 bash <<EOF\ncd /data\nnohup python train.py &\nEOF',
    'ssh -t gpu1 "setsid python train.py"',
])
def test_raw_remote_jobs_are_denied_however_they_are_written(command):
    out = _bash(guardrails.Guardrails(), command)
    assert out and "Blocked" in str(out), command


@pytest.mark.parametrize("command", [
    'ssh gpu1 "ls /data" && echo done',
    'ssh gpu1 "python check.py > log 2>&1"',
    'ssh gpu1 "cat a.txt |& head"',
    'nohup python local.py &',          # local, not over ssh: the local-job tool is a different rule
])
def test_ordinary_ssh_and_local_commands_pass(command):
    out = _bash(guardrails.Guardrails(), command)
    assert not (out and "raw ssh" in str(out)), (command, out)


@pytest.mark.parametrize("command", [
    'ssh h "pkill -f train.py"', 'ssh h "pgrep --full train.py"', 'ssh h "pkill --full train"',
    'ssh h "pgrep -c -f train.py"',
])
def test_remote_self_matching_pgrep_is_denied(command):
    out = _bash(guardrails.Guardrails(), command)
    assert out and "pgrep" in str(out), command


def test_a_bracketed_pattern_over_ssh_passes():
    out = _bash(guardrails.Guardrails(), 'ssh h "ps -eo args | grep \'[t]rain.py\'"')
    assert not (out and "Blocked" in str(out))


# ── the breaker never denies host tools ─────────────────────────────────────

def test_host_reads_and_monitor_are_never_denied_and_host_writes_reset():
    g = guardrails.Guardrails()
    for _ in range(6):
        assert _call(g, "Read", {"file_path": "/x"}) == {}
        assert _call(g, "Monitor", {"command": "until test -f done; do sleep 2; done"}) == {}
    a = {"slug": "p"}
    assert _call(g, "mcp__scivo__get_paper_state", a) == {}
    assert _call(g, "mcp__scivo__get_paper_state", a) == {}
    assert _call(g, "Edit", {"file_path": "/x", "old_string": "a", "new_string": "b"}) == {}
    assert _call(g, "mcp__scivo__get_paper_state", a) == {}      # the edit reset the count


def test_hardware_in_memory_is_a_deny_not_an_ask():
    g = guardrails.Guardrails()
    out = asyncio.run(g.on_memory_write(
        {"tool_name": "mcp__scivo__append_project_memory",
         "tool_input": {"note": "the B200 box at 10.0.0.7 has 96 cores"}}, None, None))
    assert out and "Blocked" in str(out)


# ── a command that keeps failing the same way ────────────────────────────────
# A local model spent ten hours on `ssh node2 'docker cp …'` every 7.8 s, each
# run failing with the same message, and nothing stopped it: Bash is never
# denied and the per-turn counters are cleared between turns (user,
# 2026-10-10). These tests are that incident, and the cases the rule must not
# break.


def _bash_call(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


LOOP = ("ssh node2 'docker cp comfyui-h3:/opt/ComfyUI/input/krea_bus_girl_v3.png "
        "/tmp/probe_bus_v3.png'")
SAME_ERR = ("Exit code 1 cp: '/tmp/probe_bus_v3.png'와(과) '/tmp/probe_bus_v3.png'은(는) "
            "동일한 파일입니다")


async def _pre(rails, payload):
    return await rails.on_any_tool(payload, None, None)


async def _fail(rails, payload, error):
    return await rails.on_tool_failure({**payload, "error": error}, None, None)


@pytest.mark.asyncio
async def test_a_command_that_fails_the_same_way_is_stopped_on_the_fourth_try():
    rails = guardrails.Guardrails()
    for _ in range(3):
        assert await _pre(rails, _bash_call(LOOP)) == {}       # allowed to try
        await _fail(rails, _bash_call(LOOP), SAME_ERR)
        rails.new_turn()                                   # one retry per turn
    out = await _pre(rails, _bash_call(LOOP))
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "failed 3 times with the same error" in decision["permissionDecisionReason"]
    assert "동일한 파일" in decision["permissionDecisionReason"]
    assert rails.looping == "Bash"


@pytest.mark.asyncio
async def test_a_failure_that_changes_is_progress_not_a_loop():
    rails = guardrails.Guardrails()
    for err in ("No such file", "Permission denied", "Connection refused", "Host unreachable"):
        await _fail(rails, _bash_call(LOOP), err)
        rails.new_turn()
    assert await _pre(rails, _bash_call(LOOP)) == {}


@pytest.mark.asyncio
async def test_a_success_clears_the_strikes():
    rails = guardrails.Guardrails()
    for _ in range(3):
        await _fail(rails, _bash_call(LOOP), SAME_ERR)
    await rails.on_tool_success({**_bash_call(LOOP), "tool_response": "ok"}, None, None)
    assert await _pre(rails, _bash_call(LOOP)) == {}


@pytest.mark.asyncio
async def test_the_user_interrupting_is_not_a_failure():
    rails = guardrails.Guardrails()
    for _ in range(5):
        await rails.on_tool_failure(
            {**_bash_call(LOOP), "error": "interrupted", "is_interrupt": True}, None, None)
    assert await _pre(rails, _bash_call(LOOP)) == {}


@pytest.mark.asyncio
async def test_a_different_command_is_counted_on_its_own():
    rails = guardrails.Guardrails()
    for _ in range(3):
        await _fail(rails, _bash_call(LOOP), SAME_ERR)
    other = _bash_call("ssh node2 'docker exec comfyui-h3 curl -s http://127.0.0.1:8189/object_info'")
    assert await _pre(rails, other) == {}


@pytest.mark.asyncio
async def test_old_failures_are_forgotten():
    rails = guardrails.Guardrails()
    key = ("Bash", guardrails._arguments(_bash_call(LOOP)))
    for _ in range(3):
        await _fail(rails, _bash_call(LOOP), SAME_ERR)
    # Push the record past the window: a retry an hour later starts fresh.
    rails.failures[key][2] -= guardrails.FAILURE_WINDOW + 1
    assert await _pre(rails, _bash_call(LOOP)) == {}


@pytest.mark.asyncio
async def test_watching_a_job_still_works_until_it_fails_identically():
    """`tail_remote_log` is exempt from the repeat rule on purpose — but an
    unreachable host failing the same way is not a job to watch."""
    rails = guardrails.Guardrails()
    poll = {"tool_name": "mcp__scivo__tail_remote_log",
            "tool_input": {"alias": "node2", "log_path": "run.log"}}
    for _ in range(10):
        assert await _pre(rails, poll) == {}               # changing output
    for _ in range(3):
        await _fail(rails, poll, "ssh: connect to host node2 port 22: No route to host")
    out = await _pre(rails, poll)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "tail_remote_log" in out["hookSpecificOutput"]["permissionDecisionReason"]
