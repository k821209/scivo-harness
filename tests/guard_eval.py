"""Replay the guard's scenario corpus — over this tree, or over this tree and earlier commits.

    python tests/guard_eval.py                 # this working tree
    python tests/guard_eval.py HEAD~5 v-old    # this tree beside those commits

Every scenario in tests/data/guard_scenarios.json is one shell command handed
to the guard exactly as a session hands it one, with what the guard should do
and why it matters:

    must_stop   letting it through is a bypass — the count that must be zero
    redundant   stopping it protects nothing and costs the model a round trip
                and the reader a refusal to wade through
    (none)      ordinary work

A guard is easy to make stricter and easy to make quieter; it is hard to see
which one a change did. Reading the two counts side by side for the commit
before and the commit after is the point of this file. tests/
test_guard_scenarios.py runs the same replay on the working tree and fails on
any bypass or any decision that differs from the label.

The method — labelled scenarios replayed against the working tree and against
earlier refs, with redundant denials and bypasses counted separately — is
from affaan-m/ECC's GateGuard evaluation (docs/gateguard/evaluation.md, MIT,
read 2026-11-19). The scenarios, the guard and this code are ours.

What this does not measure: whether stopping a command changes what the model
does next. It checks what the guard decides, not what the decision is worth.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "data" / "guard_scenarios.json"
# The guard asks which processes are "this session". In a replay that must be
# a fixed answer, not whatever happens to be running the replay.
SESSION = ["/home/u/.scivo/bin/python3.11 -m scivo_harness --resume abc --control --profile full",
           "claude --output-format stream-json --input-format stream-json --verbose"]


def scenarios() -> list[dict]:
    return json.loads(CORPUS.read_text(encoding="utf-8"))["scenarios"]


def decide(commands: list[str]) -> list[dict]:
    """What the importable `scivo_harness.guardrails` does with each command."""
    from scivo_harness import guardrails

    real = guardrails._own_cmdlines
    guardrails._own_cmdlines = lambda: list(SESSION)

    async def one(command: str) -> dict:
        rails = guardrails.Guardrails(root="/tmp")
        rails.loaded = {"submit_remote_job"}
        out = await rails.on_bash({"tool_input": {"command": command}}, "replay", None)
        hook = out.get("hookSpecificOutput") or {}
        decision = hook.get("permissionDecision") or "pass"
        said = str(hook.get("permissionDecisionReason") or "")
        return {"decision": decision if decision in ("deny", "ask") else "pass", "chars": len(said)}

    async def every() -> list[dict]:
        return [await one(command) for command in commands]
    try:
        return asyncio.run(every())
    finally:
        guardrails._own_cmdlines = real      # a replay must not leave the guard blind to itself


def score(rows: list[dict], decisions: list[dict]) -> dict:
    stopped = [(row, got) for row, got in zip(rows, decisions) if got["decision"] != "pass"]
    return {
        "stopped": len(stopped),
        "bypasses": [row["id"] for row, got in zip(rows, decisions)
                     if row["label"] == "must_stop" and got["decision"] == "pass"],
        "redundant": [row["id"] for row, got in stopped if row["label"] == "redundant"],
        "mismatches": [f"{row['id']}: {got['decision']} (expected {row['expect']})"
                       for row, got in zip(rows, decisions) if got["decision"] != row["expect"]],
        # What the refusals cost the conversation, at four characters a token.
        "refusal_tokens": sum(got["chars"] for _row, got in stopped) // 4,
    }


def at_ref(ref: str, commands: list[str]) -> list[dict]:
    """The same replay with the guard as it was at a commit."""
    root = HERE.parent
    with tempfile.TemporaryDirectory() as scratch:
        archive = subprocess.run(["git", "-C", str(root), "archive", ref, "src/scivo_harness"],
                                 capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", scratch], input=archive, check=True)
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2]);"
                "import guard_eval; print(json.dumps(guard_eval.decide(json.load(sys.stdin))))")
        done = subprocess.run([sys.executable, "-c", code, str(HERE), str(Path(scratch) / "src")],
                              input=json.dumps(commands), capture_output=True, text=True, check=True)
        return json.loads(done.stdout)


def main(refs: list[str]) -> int:
    rows = scenarios()
    commands = [row["command"] for row in rows]
    sys.path.insert(0, str(HERE.parent / "src"))
    columns = [("working tree", score(rows, decide(commands)))]
    for ref in refs:
        columns.append((ref, score(rows, at_ref(ref, commands))))
    labels = {"must_stop": sum(r["label"] == "must_stop" for r in rows),
              "redundant": sum(r["label"] == "redundant" for r in rows)}
    print(f"{len(rows)} scenarios: {labels['must_stop']} must be stopped, "
          f"{labels['redundant']} would be stopped for nothing, "
          f"{len(rows) - sum(labels.values())} ordinary\n")
    width = max(len(name) for name, _ in columns) + 2
    print(" " * 34 + "".join(f"{name:>{width}}" for name, _ in columns))
    for key, title in (("stopped", "stopped (refused or asked)"), ("bypasses", "must-stop let through"),
                       ("redundant", "stopped for nothing"), ("mismatches", "differs from the label"),
                       ("refusal_tokens", "tokens of refusal text")):
        cells = [value[key] if isinstance(value[key], int) else len(value[key]) for _n, value in columns]
        print(f"{title:<34}" + "".join(f"{cell:>{width}}" for cell in cells))
    for name, value in columns:
        for key in ("bypasses", "redundant"):
            if value[key]:
                print(f"\n{name} — {key}: " + ", ".join(value[key]))
    return 1 if columns[0][1]["bypasses"] or columns[0][1]["mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
