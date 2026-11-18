"""The guard, replayed over its scenario corpus.

Two counts are held here, and they pull against each other: a command that
must be stopped and is let through (a bypass), and a command stopped for
nothing (a redundant refusal). The first has always been tested. The second
was never counted until the guard was replayed over 13,964 real shell commands
from this machine's sessions (2026-11-19): 11 distinct commands were stopped
and 7 of them should not have been — the words were data, in a grep pattern,
a commit message, a script's string literals, a prompt for another program.

tests/guard_eval.py prints the same counts for this tree beside any earlier
commit, which is how a change to the guard is judged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_eval  # noqa: E402

from scivo_harness import guardrails as G  # noqa: E402

ROWS = guard_eval.scenarios()
DECISIONS = guard_eval.decide([row["command"] for row in ROWS])
SCORE = guard_eval.score(ROWS, DECISIONS)


def test_the_corpus_is_one_a_reader_can_trust():
    ids = [row["id"] for row in ROWS]
    assert len(ids) == len(set(ids))
    for row in ROWS:
        assert row["expect"] in {"deny", "ask", "pass"}, row["id"]
        assert row["label"] in {"must_stop", "redundant", None}, row["id"]
        assert row["why"].strip(), row["id"]
        # A label and an expectation that disagree would make both counts lie.
        assert (row["label"] == "must_stop") == (row["expect"] != "pass"), row["id"]
    assert sum(row["label"] == "must_stop" for row in ROWS) >= 30
    assert sum(row["label"] == "redundant" for row in ROWS) >= 12
    assert sum(row["label"] is None for row in ROWS) >= 10


def test_nothing_that_must_be_stopped_gets_through():
    assert SCORE["bypasses"] == []


def test_nothing_is_stopped_for_nothing():
    assert SCORE["redundant"] == []


@pytest.mark.parametrize("row, got", list(zip(ROWS, DECISIONS)), ids=[row["id"] for row in ROWS])
def test_each_scenario_is_decided_as_labelled(row, got):
    assert got["decision"] == row["expect"], f"{row['why']}\n{row['command']}"


def test_a_refusal_says_what_to_do_instead():
    for row, got in zip(ROWS, DECISIONS):
        if got["decision"] != "pass":
            assert got["chars"] > 150, row["id"]


def test_the_commit_before_this_one_stopped_nine_for_nothing_and_let_nothing_through():
    """The comparison the corpus exists for, pinned once so the claim in the
    commit message can be re-run: the guard at 8f6ac93 over the same file."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    known = subprocess.run(["git", "-C", str(root), "cat-file", "-e", "8f6ac93^{commit}"],
                           capture_output=True).returncode == 0
    if not known:
        pytest.skip("the earlier commit is not in this checkout (a shallow clone, or an archive)")
    before = guard_eval.score(ROWS, guard_eval.at_ref("8f6ac93", [row["command"] for row in ROWS]))
    assert before["bypasses"] == []
    assert sorted(before["redundant"]) == sorted([
        "grep-for-the-words", "commit-message-about-it", "note-text-about-it", "node-test-strings",
        "python-c-test-strings", "prompt-for-another-program", "echo-an-example",
        "quoted-word-as-an-argument", "echo-inside-a-loop"])
    assert before["refusal_tokens"] > SCORE["refusal_tokens"]


# --- how a line is read ------------------------------------------------------

def _remote(line: str) -> list[str]:
    return [call["remote"].strip() for call in G.remote_calls(G.executable_text(line))]


def test_ssh_is_a_remote_call_only_as_a_word_of_its_own():
    assert _remote('ssh gpu "ls /data"') == ['ssh gpu "ls /data"']
    assert _remote('/usr/bin/ssh gpu ls') == ['/usr/bin/ssh gpu ls']
    assert _remote('grep -n "ssh gpu nohup" notes.md') == []
    assert _remote("git commit -m 'never ssh host nohup job &'") == []
    assert _remote("rsync -az -e 'ssh -p 2222' a/ gpu:/b/") == []
    assert _remote("ssh-keygen -t ed25519 && ls ~/.ssh") == []
    assert _remote("NOTE='ssh gpu nohup x &'") == []


def test_what_follows_the_ssh_word_is_the_remote_sides_and_nothing_else_is():
    assert _remote("cd /x && ssh gpu 'tail log' | head -3; echo ok &") == ["ssh gpu 'tail log'"]
    assert G.launches_remote_job("cd /x && ssh gpu 'tail log' | head -3; echo ok &") is False
    assert G.launches_remote_job("ssh gpu 'tail log' &") is True


def test_a_program_that_only_shows_or_searches_text_runs_nothing_remotely():
    for line in ("echo ssh gpu nohup job &", "printf '%s\\n' ssh gpu nohup", "grep -rn ssh nohup ."):
        assert _remote(line) == [], line
        assert G.launches_remote_job(line) is False, line


def test_a_comment_is_not_a_command():
    assert _remote("# ssh gpu nohup job &\nls") == []
    assert _remote("ls # then ssh gpu nohup job") == []


def test_a_heredoc_fed_to_ssh_goes_with_it_and_one_fed_to_a_file_does_not():
    fed = "ssh gpu bash <<'EOF'\ncd /data\nnohup python train.py &\nEOF\necho after"
    (call,) = G.remote_calls(G.executable_text(fed))
    assert "nohup python train.py" in call["remote"]
    assert G.launches_remote_job(G.executable_text(fed)) is True
    written = "cat > notes.md <<'EOF'\nssh gpu nohup job &\nEOF"
    assert G.launches_remote_job(G.executable_text(written)) is False


def test_one_shell_deeper_and_inside_a_substitution_are_still_read():
    assert G.launches_remote_job("bash -c 'ssh gpu \"nohup x &\"'") is True
    assert G.launches_remote_job("eval 'ssh gpu \"nohup x &\"'") is True
    assert G.launches_remote_job('PID=$(ssh gpu "nohup x & echo \\$!")') is True
    assert G.launches_remote_job('echo "pid: $(ssh gpu \'nohup x & echo $!\')"') is True
    assert G.launches_remote_job("X=`ssh gpu 'nohup x &'`") is True
    assert G.launches_remote_job("bash -c 'echo \"ssh gpu nohup x\"'") is False


def test_left_running_from_this_side_counts_too():
    assert G.launches_remote_job("nohup ssh gpu 'python train.py' > out.log 2>&1 &") is True
    assert G.launches_remote_job("setsid ssh gpu 'python train.py'") is True
    assert G.launches_remote_job("ssh gpu 'python train.py' > out.log 2>&1") is False


def test_redirections_and_double_ampersands_are_not_backgrounding():
    for line in ("ssh gpu 'make 2>&1 | tail'", "ssh gpu 'a && b'", "ssh gpu 'make |& tee log'",
                 "ssh gpu 'run &> log'", "ssh gpu ls 2>&1"):
        assert G.launches_remote_job(line) is False, line


@pytest.mark.parametrize("line", [
    'ssh gpu "nohup python x.py &',            # a quote that never closes
    "ssh gpu 'nohup x &' $(echo",              # a substitution that never closes
    "ssh gpu bash <<EOF\nnohup x &\n",         # a heredoc that never closes
    "ssh gpu `nohup x &",                      # a backtick that never closes
])
def test_a_line_that_cannot_be_read_is_judged_by_the_old_patterns_never_passed(line):
    with pytest.raises(G._Unreadable):
        G.remote_calls(line)
    assert G.launches_remote_job(line) is True


def test_a_reader_that_fails_does_not_become_a_pass(monkeypatch):
    def broken(_line, _depth=0):
        raise RuntimeError("a bug in the reader")
    monkeypatch.setattr(G, "remote_calls", broken)
    assert G.launches_remote_job('ssh gpu "nohup x &"') is True
    assert G.matches_patterns_remotely('ssh gpu "pkill -f x"') is True
    assert G.launches_remote_job("ls") is False


def test_nesting_too_deep_to_follow_is_unreadable_not_clean():
    line = "ssh gpu 'nohup x &'"
    for _ in range(G._NESTING_LIMIT + 2):
        line = "bash -c " + __import__("shlex").quote(line)
    assert G.launches_remote_job(line) is True
