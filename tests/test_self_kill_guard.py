"""A pattern kill that would stop the session running it.

The remote guard required the ssh prefix, so the same command on THIS machine
went straight through — and it sends SIGTERM by default, which is exit 143,
which is how a session died with "Cannot write to terminated process" (user,
2026-11-09, asking whether the model had done it to itself). The question was
fair: nothing stopped it.
"""
from __future__ import annotations

import pytest

from scivo_harness import guardrails
from scivo_harness.guardrails import LETHAL_WORDS, kills_this_session

# What a real session's own processes look like, program and first arguments.
OURS = [
    "/home/u/.scivo/bin/python3 -m scivo_harness",
    "/home/u/.scivo/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude -p --input-format",
    "/home/u/.scivo/bin/python3 -m co_scientist_local",
]


@pytest.fixture
def ours(monkeypatch):
    monkeypatch.setattr(guardrails, "_own_cmdlines", lambda: list(OURS))


@pytest.mark.parametrize("command", [
    "pkill -f claude",
    "pkill -9 -f claude",
    "pkill --full claude",
    "killall claude",
    "pkill -f scivo_harness",
    "pkill -f co_scientist_local",
    "pkill -f python3",
    "cd /tmp && pkill -f claude",
])
def test_it_catches_a_pattern_that_hits_our_own_processes(ours, command):
    assert kills_this_session(command) is not None, command


@pytest.mark.parametrize("command", [
    "pkill -f train.py",
    "killall ffmpeg",
    "pkill -f 'bcftools view'",
    "kill 12345",
    "kill -9 $(cat .pidfile)",
    "pkill",                      # no pattern at all
    "pkill -f",                   # a flag and nothing else
    "echo pkill is a command",    # not an invocation of it
])
def test_it_leaves_a_legitimate_kill_alone(ours, command):
    # Killing a stray training job is ordinary work; a guard that blocks it
    # would be turned off, and then it protects nothing.
    assert kills_this_session(command) is None, command


def test_the_whole_command_line_is_never_what_gets_compared():
    """The CLI is started with the entire system prompt as an argument.

    Tens of thousands of characters containing "python", "analysis", "train".
    Tested at the layer that drops it — the reader — because an earlier
    version of this test monkeypatched the reader and then asserted about the
    comparator, which cannot see the problem.
    """
    argv = ["/u/claude", "-p", "--append-system-prompt",
            "you are a co-scientist; run train.py and ffmpeg over the analysis"]
    program = guardrails._program_of(argv)
    assert program == "/u/claude -p --append-system-prompt"
    assert "train.py" not in program and "ffmpeg" not in program


def test_a_pattern_too_short_to_mean_anything_is_ignored(ours):
    # "is" matched `co_scientist_local` and turned `echo pkill is a command`
    # into a refusal.
    assert kills_this_session("echo pkill is a command") is None
    assert kills_this_session("pkill -f py") is None


def test_the_real_reader_keeps_only_the_program_and_two_arguments():
    lines = guardrails._own_cmdlines()
    assert lines, "it must be able to see this process at all"
    for line in lines:
        assert len(line.split()) <= 3, line


def test_without_proc_it_falls_back_to_words_it_cannot_allow(monkeypatch):
    # Off Linux there is nothing to ask, and "could not look" must not become
    # "nothing of ours matches".
    monkeypatch.setattr(guardrails, "_own_cmdlines", lambda: [])
    for word in LETHAL_WORDS:
        assert kills_this_session(f"pkill -f {word}") is not None, word
    assert kills_this_session("pkill -f train.py") is None


def test_the_quotes_around_a_pattern_are_not_part_of_it(ours):
    assert kills_this_session("pkill -f 'claude'") is not None
    assert kills_this_session('pkill -f "claude"') is not None
