"""A turn on a local model has a budget of tool calls.

msja, 2026-11-14: "멈췄던 작업 계속 진행" reached a model in a conversation that
no longer held the work, and it looked for it — 127 calls in eighteen minutes,
the last forty a cycle of the same six reads. Two days earlier one turn made
1,146. Every rule that tried to RECOGNISE the loop was walked round by
commands that differ a little each time; this one counts.
"""
from __future__ import annotations

import asyncio

from scivo_harness import guardrails
from scivo_harness.providers import TURN_CALLS_DEFAULT, Provider


def _call(rails, name, args, n=0, result=None):
    payload = {"tool_name": name, "tool_input": args}
    out = asyncio.run(rails.on_any_tool(payload, f"id-{n}", None))
    if "Blocked" not in str(out) and result is not None:
        response = ({"stdout": result, "stderr": ""} if name == "Bash"
                    else [{"type": "text", "text": result}])
        asyncio.run(rails.on_tool_success({**payload, "tool_response": response}, None, None))
    return out


def _decision(out) -> str:
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision", "")


def _budgeted(n=40):
    rails = guardrails.Guardrails()
    rails.turn_budget = n
    return rails


def test_a_local_provider_has_a_budget_and_claude_does_not():
    local = Provider(name="gpu", base_url="http://localhost:8192", model="local")
    assert local.lean_profile and local.turn_budget == TURN_CALLS_DEFAULT
    assert Provider(name="anthropic").turn_budget is None
    assert Provider(name="gpu", base_url="http://x", model="local", turn_calls=0).turn_budget is None
    assert Provider(name="gpu", base_url="http://x", model="local", turn_calls=75).turn_budget == 75
    assert Provider(name="anthropic", turn_calls=25).turn_budget == 25      # asked for, so given


def test_the_model_is_told_at_three_quarters_and_refused_at_the_limit():
    rails = _budgeted(40)
    notes = []
    for n in range(40):
        out = _call(rails, "Bash", {"command": f"ls dir{n}"}, n, result=f"file{n}")
        assert _decision(out) != "deny", f"refused at call {n + 1}"
        if "additionalContext" in str(out):
            notes.append(n + 1)
    assert notes == [30]
    assert not rails.looping
    out = _call(rails, "Bash", {"command": "ls one-more"}, 41)
    assert _decision(out) == "deny"
    said = str(out)
    assert "40 tool calls" in said and "not an error to" in said and "Write to the user now" in said
    assert not rails.looping, "the first refusal leaves room to answer in words"


def test_a_model_that_keeps_calling_past_the_limit_is_stopped():
    rails = _budgeted(10)
    for n in range(10):
        _call(rails, "Bash", {"command": f"ls dir{n}"}, n, result="x")
    for n in range(10, 10 + guardrails.BUDGET_OVERRUN):
        assert _decision(_call(rails, "Bash", {"command": f"ls dir{n}"}, n)) == "deny"
    assert rails.looping
    assert "10 tool calls in one turn" in rails.stop_reason
    assert "계속" in rails.stop_hint and "turn_calls" in rails.stop_hint


def test_asking_the_user_is_never_counted_or_refused():
    rails = _budgeted(3)
    for n in range(8):
        assert _call(rails, "AskUserQuestion", {"questions": []}, n) == {}
    assert rails.turn_calls == 0


def test_the_next_turn_starts_from_nothing():
    rails = _budgeted(5)
    for n in range(9):
        _call(rails, "Bash", {"command": f"ls dir{n}"}, n, result="x")
    assert rails.looping
    rails.new_turn()
    assert rails.turn_calls == 0 and not rails.looping
    assert _decision(_call(rails, "Bash", {"command": "ls again"}, 99, result="x")) != "deny"


def test_a_session_without_a_budget_is_untouched():
    rails = guardrails.Guardrails()
    for n in range(200):
        assert _call(rails, "Bash", {"command": f"ls dir{n}"}, n, result="x") == {}
    assert not rails.looping and rails.turn_calls == 0


# ── the same answer, counted over the whole turn ────────────────────────────

POLL = {"command": "ssh h 'python3 -c \"print(history())\"'"}


def test_a_poll_with_a_different_command_every_third_call_is_refused():
    """The log's own shape — poll, poll, poll, ls, poll, poll, poll, md5sum —
    ran 41 times without one refusal: each new command reset the count."""
    rails = _budgeted(200)
    refused_at = None
    for n in range(41):
        if n % 4 == 3:
            _call(rails, "Bash", {"command": f"ssh h 'ls -la dir{n}'"}, n, result=f"listing {n}")
            continue
        out = _call(rails, "Bash", POLL, n, result="status: success")
        if _decision(out) == "deny":
            refused_at = n
            break
    assert refused_at is not None and refused_at <= 8
    assert "returned exactly the same thing 5 times" in str(out)


def test_another_servers_tool_is_counted_the_same_way_and_ours_is_not():
    rails = _budgeted(200)
    query = {"query": "dw_eye"}
    outs = []
    for n in range(12):
        outs.append(_decision(_call(rails, "mcp__persona__query_memories", query, n, result="2 rows")))
        _call(rails, "mcp__persona__query_memories", {"query": f"other {n}"}, 100 + n, result=f"r{n}")
    assert "deny" in outs and outs.index("deny") <= 5
    ours = _budgeted(200)
    for n in range(8):
        # a write in between is what makes the same preview a new question
        _call(ours, "mcp__scivo__update_slide", {"slide_id": "s", "code": f"v{n}"}, 200 + n, result="ok")
        assert _decision(_call(ours, "mcp__scivo__preview_slide", {"slide_id": "s"}, n,
                               result="/tmp/s.png")) != "deny"


def test_an_answer_that_changes_or_a_file_edit_starts_the_count_again():
    rails = _budgeted(200)
    for n in range(12):
        assert _decision(_call(rails, "Bash", POLL, n, result=f"epoch {n}")) != "deny"
    edited = _budgeted(200)
    for n in range(12):
        assert _decision(_call(edited, "Bash", {"command": "python check.py"}, n, result="FAIL")) != "deny"
        _call(edited, "Edit", {"file_path": "/x", "old_string": "a", "new_string": "b"}, 300 + n)


def test_without_a_budget_the_whole_turn_rule_is_off():
    """Six runs of one failing test after five shell fixes is slow work."""
    rails = guardrails.Guardrails()
    for n in range(8):
        assert _decision(_call(rails, "Bash", {"command": "python check.py"}, n, result="FAIL")) != "deny"
        _call(rails, "Bash", {"command": f"sed -i 's/a{n}/b/' f.py"}, 400 + n, result="")
