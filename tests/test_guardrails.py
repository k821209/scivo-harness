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
    key = guardrails._key(_bash_call(LOOP))
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


# ── a heredoc body is data, not a command ────────────────────────────────────
# `cat > notes.rs <<'EOF' … ssh … EOF` was blocked as a raw remote job because
# a Korean comment in the file mentioned ssh. The way round it was the Write
# tool, which improves provenance not at all (feedback 504eb4a8d21e).


REPORTED = """cat > /tmp/probe.txt <<'EOF'
이 문서는 ssh 로 접속한 환경을 설명한다.
EOF"""


def test_the_reported_local_file_write_passes():
    assert _bash(guardrails.Guardrails(), REPORTED) == {}


def test_a_heredoc_that_names_remote_backgrounding_still_passes():
    cmd = ("cat > src/ime.rs <<'EOF'\n"
           "//! 조합 중인 음절을 보여줄 수 없고, ssh 로 들어간 세션에는 IME 가 없다.\n"
           "//! nohup 으로 띄운 작업도 마찬가지다.\n"
           "EOF\n"
           "cargo test")
    assert _bash(guardrails.Guardrails(), cmd) == {}


def test_a_real_remote_job_is_still_blocked_beside_a_heredoc():
    """Stripping the body must not disarm the check for the command itself."""
    cmd = ("cat > run.sh <<'EOF'\n"
           "echo hello\n"
           "EOF\n"
           "ssh gpu1 \"nohup python train.py &\"")
    out = _bash(guardrails.Guardrails(), cmd)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "no run record" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_quoted_arguments_are_still_read():
    """The dangerous part of `ssh host "nohup …"` lives inside the quotes, so
    quotes are deliberately not stripped the way heredoc bodies are."""
    out = _bash(guardrails.Guardrails(), 'ssh gpu1 "cd /data; nohup python train.py &"')
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_remote_pgrep_inside_a_heredoc_is_not_a_remote_pgrep():
    cmd = ("cat > README.md <<'EOF'\n"
           "Do not run `ssh host \"pkill -f train.py\"` — it kills your own session.\n"
           "EOF")
    assert _bash(guardrails.Guardrails(), cmd) == {}


def test_every_heredoc_form_is_handled():
    for opener, closer in (("<<'EOF'", "EOF"), ('<<"EOF"', "EOF"),
                           ("<<EOF", "EOF"), ("<<-EOF", "\tEOF"), ("<<PY", "PY")):
        cmd = f"cat > f.txt {opener}\nssh host nohup thing &\n{closer}"
        assert _bash(guardrails.Guardrails(), cmd) == {}, opener


def test_an_unterminated_heredoc_is_left_alone():
    """No closing tag means no body to strip; the text is scanned as before,
    which errs toward blocking rather than toward missing a remote job."""
    out = _bash(guardrails.Guardrails(), "cat > f.txt <<'EOF'\nssh gpu nohup train &")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_heredoc_fed_to_ssh_is_the_remote_command_and_is_still_read():
    """`ssh host bash <<EOF … EOF` runs its body on the other machine. The
    first version of the heredoc fix stripped it and hid a real remote job."""
    cmd = "ssh gpu1 bash <<EOF\ncd /data\nnohup python train.py &\nEOF"
    out = _bash(guardrails.Guardrails(), cmd)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_local_heredoc_before_an_ssh_heredoc_is_judged_separately():
    cmd = ("cat > notes.md <<'EOF'\n"
           "ssh 로 접속한 세션 이야기.\n"
           "EOF\n"
           "ssh gpu1 bash <<EOF\nnohup python train.py &\nEOF")
    out = _bash(guardrails.Guardrails(), cmd)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# ── EC numbers are not IP addresses ──────────────────────────────────────────
# `EC 3.2.1.35` is four dot-separated numbers, so the dotted-quad regex denied
# a note about hyaluronidase as if it carried a machine address, and told the
# author to register a server that does not exist (reported 2026-10-15). The
# guard still has to hold for real hardware, which is the second half of this.

EC_NOTES = [
    "히알루로니다제(EC 3.2.1.35)는 모든 분류군에서 0으로 집계됨",
    "EC 3.2.1.35, 3.1.1.4, 1.4.3.2 for the venom enzyme census",
    "Table 2 groups the Tox-Prot entries by EC number: 3.4.24.21 dominates",
    "phospholipase A2 (EC:3.1.1.4) is the most frequent venom enzyme",
]

IP_NOTES = [
    "the B200 box at 10.0.0.7 has 96 cores",
    "run it on 192.168.0.9",
    "the render host is 203.0.113.7:22",
    "copy from 172.16.4.1 when the enzyme assay finishes",
]


def _memory_write(note: str) -> dict:
    return asyncio.run(guardrails.Guardrails().on_memory_write(
        {"tool_name": "mcp__scivo__append_project_memory",
         "tool_input": {"note": note}}, None, None))


def test_ec_numbers_are_not_blocked_as_addresses():
    for note in EC_NOTES:
        assert _memory_write(note) == {}, note


def test_real_addresses_are_still_blocked():
    for note in IP_NOTES:
        out = _memory_write(note)
        assert out and "Blocked" in str(out), note
        assert "an IP address" in str(out), note


def test_a_dotted_quad_that_cannot_be_an_address_is_ignored():
    # 350 is not an octet, so there is nothing to redirect to the registry.
    assert _memory_write("the assay read 3.2.1.350 on the plate") == {}
    # A version string carrying a letter prefix is not a quad either.
    assert _memory_write("built with blastn v2.5.0.1") == {}


def test_the_other_compute_specs_still_fire():
    for note, label in [("the node has an H100", "a GPU model"),
                        ("48 cores free", "a core count"),
                        ("512 GB RAM installed", "a memory size"),
                        ("ssh kang@node2 to check", "an ssh target")]:
        assert label in str(_memory_write(note)), note


# ── a loop that steps around the refusal (feedback f5c4b6108ef6) ─────────────
#
# A local model collecting a three-part briefing asked the same queries dozens
# of times; the breaker refused, the model moved to the next query and came
# back, and the person stopped it by hand. Each pattern below ran to the end
# of its list before the fix, with the turn never stopped.

def _drive(rails, calls, output="3 rows"):
    """Make the calls the way a session does — the pre-check, and a result for
    whatever was allowed — until the turn is stopped. Returns (ran, refused)."""
    ran = refused = 0
    for name, args in calls:
        payload = {"tool_name": name, "tool_input": args}
        out = asyncio.run(rails.on_any_tool(payload, None, None))
        if "Blocked" in str(out):
            refused += 1
        else:
            ran += 1
            response = ({"stdout": output, "stderr": ""} if name == "Bash"
                        else [{"type": "text", "text": output}])
            asyncio.run(rails.on_tool_success({**payload, "tool_response": response}, None, None))
        if rails.looping:
            break
    return ran, refused


_QA, _QB, _QC = ({"sql": f"SELECT * FROM {t}"} for t in ("calendar", "todos", "reminders"))
_BA, _BB, _BC = ({"command": f'sqlite3 p.db "SELECT * FROM {t}"'}
                 for t in ("calendar", "todos", "reminders"))


def test_refusals_add_up_across_different_calls():
    """A A A(refused) B B B(refused) C C C(refused): 15 refusals in 45 calls
    before, and no stop — no single call reached five."""
    rails = guardrails.Guardrails()
    calls = [("mcp__persona__query", q) for q in (_QA, _QA, _QA, _QB, _QB, _QB, _QC, _QC, _QC)] * 5
    ran, refused = _drive(rails, calls)
    assert rails.looping and (ran, refused) == (6, 3)
    assert "3 repeated calls were refused" in rails.stop_reason


def test_a_query_tool_on_another_server_does_not_wipe_the_counts():
    """`query` is not a read by its name, so each call counted as a write and
    forgot the others: A B C in rotation, 45 calls, not one refusal."""
    rails = guardrails.Guardrails()
    ran, refused = _drive(rails, [("mcp__persona__query", q) for q in (_QA, _QB, _QC)] * 15)
    assert rails.looping and ran <= 9 and refused == 3


def test_the_same_command_with_the_same_output_is_refused():
    """Bash was never refused, at any count: 40 identical sqlite3 calls, one
    note about Monitor."""
    rails = guardrails.Guardrails()
    ran, refused = _drive(rails, [("Bash", _BA)] * 40)
    assert rails.looping and (ran, refused) == (3, 3)


def test_three_commands_in_rotation_are_stopped_too():
    rails = guardrails.Guardrails()
    ran, refused = _drive(rails, [("Bash", c) for c in (_BA, _BB, _BC)] * 13)
    assert rails.looping and ran <= 12 and refused == 3


def test_rewording_the_description_is_the_same_command():
    rails = guardrails.Guardrails()
    calls = [("Bash", {**_BA, "description": f"try number {i}"}) for i in range(40)]
    ran, refused = _drive(rails, calls)
    assert rails.looping and (ran, refused) == (3, 3)


def test_a_command_whose_output_changes_is_watching_not_looping():
    rails = guardrails.Guardrails()
    payload = {"tool_name": "Bash", "tool_input": {"command": "ssh h 'tail -1 run.log'"}}
    for i in range(12):
        out = asyncio.run(rails.on_any_tool(payload, None, None))
        assert "Blocked" not in str(out), f"refused on poll {i}"
        asyncio.run(rails.on_tool_success(
            {**payload, "tool_response": {"stdout": f"epoch {i}", "stderr": ""}}, None, None))
    assert not rails.looping


def test_test_then_fix_then_test_again_is_not_a_loop():
    """The same failing output five times is five fixes that did not work —
    each fix is a command not yet run this turn."""
    rails = guardrails.Guardrails()
    check = {"tool_name": "Bash", "tool_input": {"command": "python check.py"}}
    for i in range(6):
        assert "Blocked" not in str(asyncio.run(rails.on_any_tool(check, None, None)))
        asyncio.run(rails.on_tool_success(
            {**check, "tool_response": {"stdout": "FAIL: 2 errors", "stderr": ""}}, None, None))
        fix = {"tool_name": "Bash", "tool_input": {"command": f"sed -i 's/a{i}/b/' f.py"}}
        assert asyncio.run(rails.on_any_tool(fix, None, None)) == {}
        asyncio.run(rails.on_tool_success(
            {**fix, "tool_response": {"stdout": "", "stderr": ""}}, None, None))
    assert not rails.looping


def test_a_file_edit_in_between_lets_the_command_run_again():
    rails = guardrails.Guardrails()
    check = {"tool_name": "Bash", "tool_input": {"command": "python check.py"}}
    for _ in range(6):
        assert "Blocked" not in str(asyncio.run(rails.on_any_tool(check, None, None)))
        asyncio.run(rails.on_tool_success(
            {**check, "tool_response": {"stdout": "FAIL", "stderr": ""}}, None, None))
        _call(rails, "Edit", {"file_path": "/x", "old_string": "a", "new_string": "b"})
    assert not rails.looping


def test_a_result_the_payload_does_not_carry_is_not_called_the_same():
    """No `tool_response`: whether the output repeated cannot be told, and
    "cannot tell" must not read as "identical"."""
    rails = guardrails.Guardrails()
    payload = {"tool_name": "Bash", "tool_input": _BA}
    for _ in range(8):
        assert "Blocked" not in str(asyncio.run(rails.on_any_tool(payload, None, None)))
        asyncio.run(rails.on_tool_success(payload, None, None))
    assert not rails.looping


def test_a_refusal_hands_the_result_back_and_says_what_is_left():
    rails = guardrails.Guardrails()
    payload = {"tool_name": "mcp__persona__get_rows", "tool_input": _QA}
    for _ in range(2):
        asyncio.run(rails.on_any_tool(payload, None, None))
        asyncio.run(rails.on_tool_success(
            {**payload, "tool_response": [{"type": "text", "text": "09:00 lab meeting"}]},
            None, None))
    said = str(asyncio.run(rails.on_any_tool(payload, None, None)))
    assert "09:00 lab meeting" in said
    assert "not a temporary error" in said and "2 more refused calls ends this turn" in said


def test_a_long_result_is_cut_and_says_so():
    rails = guardrails.Guardrails()
    payload = {"tool_name": "Bash", "tool_input": _BA}
    _drive(rails, [("Bash", _BA)] * 3, output="x" * 5000)
    said = str(asyncio.run(rails.on_any_tool(payload, None, None)))
    assert "the first 1,200 of 5,000 characters" in said
    assert "x" * 1201 not in said


def test_a_new_turn_starts_the_count_again():
    rails = guardrails.Guardrails()
    _drive(rails, [("Bash", _BA)] * 40)
    assert rails.looping
    rails.new_turn()
    assert not rails.looping and rails.refusals == 0 and rails.stop_reason == ""
    assert _drive(rails, [("Bash", _BA)] * 2) == (2, 0)


# ── a refused call, made again (msja, 2026-11-12) ────────────────────────────
#
# One turn made three ssh commands 1,114 times in five hours. Every one was
# refused before it ran — 771 times by the hook file, 358 times here — and a
# refused call has no result and no failure, so no counter moved.

HOOK_FILE = ("PreToolUse:Bash hook error: [/home/u/.scivo/bin/python3 /home/u/co-sci/packages/"
             "hooks/pretool_block_ssh_nohup.py]: Blocked: raw ssh nohup to a registered server")
START = "ssh deevo-219 'bash /tmp/start_qwen.sh > /tmp/qwen.log 2>&1 &'"
PROBE = "ssh deevo-219 'if curl -s localhost:8000/health; then echo up; fi; bash /tmp/s.sh &'"


def _attempt(rails, command, refusal, n):
    """One call the way a session sees it: the pre-check, then — unless the
    loop breaker itself refused — the refusal coming back as the result."""
    payload = _bash_call(command)
    out = asyncio.run(rails.on_any_tool(payload, f"id-{n}", None))
    if "Blocked" not in str(out):
        rails.note_refused(f"id-{n}", "Bash", payload["tool_input"], refusal)
    return out


def test_the_same_refused_command_ends_the_turn_on_the_fourth_try():
    rails = guardrails.Guardrails()
    for n in range(408):
        _attempt(rails, START, HOOK_FILE, n)
        if rails.looping:
            break
    assert n == 3, f"stopped after {n + 1} attempts"
    assert "refused" in rails.stop_reason and rails.stop_hint.startswith("It was not run")


def test_three_refused_commands_in_rotation_end_it_too():
    """The turn as it was: start, probe, and the probe again under `# setup`."""
    rails = guardrails.Guardrails()
    commands = [START, PROBE, "# setup\n" + PROBE]
    for n in range(1114):
        _attempt(rails, commands[n % 3], HOOK_FILE, n)
        if rails.looping:
            break
    assert n + 1 <= guardrails.UNRUN_STOP, f"stopped after {n + 1} attempts"
    assert "refused in this turn before they ran" in rails.stop_reason


def test_a_tool_that_does_not_exist_is_not_asked_for_for_ever():
    """No PreToolUse runs for a tool the CLI does not have, so only the
    result says so — five rounds of four such calls in one msja turn."""
    rails = guardrails.Guardrails()
    said = "Error: No such tool available: mcp__scivo__list_videos."
    for n in range(20):
        rails.note_refused(f"id-{n}", "mcp__scivo__list_videos", {}, said)
        if rails.looping:
            break
    assert n == 3 and rails.looping == "list_videos"


def test_the_breakers_own_refusal_is_counted_once():
    rails = guardrails.Guardrails()
    payload = {"tool_name": "mcp__scivo__list_todos", "tool_input": {}}
    for n in range(2):
        asyncio.run(rails.on_any_tool(payload, f"id-{n}", None))
    out = asyncio.run(rails.on_any_tool(payload, "id-2", None))
    assert "Blocked" in str(out) and rails.refusals == 1
    rails.note_refused("id-2", "mcp__scivo__list_todos", {}, "PreToolUse:x hook error: Blocked")
    assert rails.unrun == 0 and not rails.failures


def test_a_new_kit_forgets_what_could_not_be_called():
    rails = guardrails.Guardrails()
    for n in range(3):
        rails.note_refused(f"id-{n}", "mcp__scivo__list_videos", {}, "No such tool available")
    rails.kit_changed({"list_videos"})
    payload = {"tool_name": "mcp__scivo__list_videos", "tool_input": {}}
    assert asyncio.run(rails.on_any_tool(payload, "id-9", None)) == {}


def test_the_ssh_refusal_does_not_name_a_tool_the_session_lacks():
    """"Use submit_remote_job" was said 358 times to a session without it."""
    rails = guardrails.Guardrails()
    rails.loaded = {"whoami", "add_video"}
    said = str(_bash(rails, START))
    assert "is not loaded in this session" in said and "tell the user" in said
    assert "Use `mcp__scivo__submit_remote_job" not in said
    rails.loaded = {"whoami", "submit_remote_job"}
    assert "Use `mcp__scivo__submit_remote_job" in str(_bash(rails, START))
    rails.loaded = None                      # not told: the message as it always was
    assert "Use `mcp__scivo__submit_remote_job" in str(_bash(rails, START))


def test_the_polling_note_is_not_given_to_a_command_that_never_ran():
    rails = guardrails.Guardrails()
    for n in range(6):
        out = asyncio.run(rails.on_any_tool(_bash_call(START), f"id-{n}", None))
        assert "Monitor" not in str(out)
    ran = guardrails.Guardrails()
    payload = _bash_call("ssh h 'tail -1 run.log'")
    notes = []
    for n in range(6):
        notes.append(str(asyncio.run(ran.on_any_tool(payload, f"id-{n}", None))))
        asyncio.run(ran.on_tool_success(
            {**payload, "tool_response": {"stdout": f"epoch {n}", "stderr": ""}}, None, None))
    assert sum("Monitor" in note for note in notes) == 1
