"""Reading a shell line well enough to say what KIND of command it is.

Two things need this and neither may trust a guess: which commands an earlier
"always" covers (permissions), and whether a command is an analysis that
should leave a run record (guardrails). Not a shell parser, and it does not
try to be — everything it is unsure of comes back as None, and both callers
treat None as the cautious answer.

Kept free of imports from the rest of the package so both can use it.
"""

from __future__ import annotations

# ── what "always" means for a shell command ─────────────────────────────────
#
# It used to mean the TOOL: answer `a` to `ls` and every later Bash command
# ran unasked for the rest of the session — `rm -rf ~/data && curl x | sh`
# went through without a prompt (review 2026-11-10, reproduced). The answer
# was given to one command and taken as an answer to all of them.
#
# It now remembers the KIND of command that was shown: the program, plus its
# subcommand where the program is a family of them (`git status` is not
# `git push`), plus the script where the program is an interpreter. A later
# command runs unasked only if every part of it is a kind already allowed —
# and only if it can be READ: anything with command substitution, a subshell,
# a heredoc or output redirected into a file is asked about, whatever it
# starts with, because `ls $(…)` and `echo x > ~/.bashrc` begin innocently.
SHELL_TOOLS = ("Bash", "Monitor")

# Programs that are a family of commands: the subcommand is part of the kind.
SUBCOMMAND_TOOLS = frozenset({
    "git", "gh", "docker", "kubectl", "npm", "npx", "pnpm", "yarn", "pip", "pip3",
    "uv", "poetry", "conda", "mamba", "micromamba", "cargo", "go", "make", "brew",
    "apt", "apt-get", "systemctl", "firebase", "gcloud", "scivo", "claude",
})
# Programs that run whatever they are handed: the script is part of the kind,
# and inline code (`-c`, `-e`) is never a kind at all.
INTERPRETERS = frozenset({
    "python", "python3", "node", "bash", "sh", "zsh", "Rscript", "perl", "ruby",
})
# Programs whose whole job is to run ANOTHER command. Remembering one of these
# would be remembering everything.
NEVER_REMEMBERED = frozenset({
    "sudo", "doas", "env", "xargs", "nohup", "time", "timeout", "watch", "eval",
    "exec", "source", ".", "nice", "ionice", "setsid", "parallel",
})
# `find` reads — until it is told to run or remove something.
_FIND_ACTS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"})
# A variable set in front of a command that changes WHICH program runs, or
# what it loads: with one of these the first word is no longer the kind.
_LOADER_VARS = frozenset({"PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
                          "PYTHONPATH", "PYTHONSTARTUP", "NODE_OPTIONS", "BASH_ENV", "ENV",
                          "GIT_SSH", "GIT_SSH_COMMAND"})
# Nothing to ask about, alone or between other commands.
HARMLESS = frozenset({"cd", "pwd", "true", "false", ":", "test", "[", "echo", "printf"})
_SSH_VALUE_FLAGS = frozenset("bcDEeFIiJLlmOopQRSWw")
_CONTROL = frozenset({"&&", "||", ";", "|", "|&", "&"})


_OPERATOR_CHARS = frozenset("();<>|&")
_WRITES = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})
_DUPLICATES = frozenset({">&", "<&"})


def shell_parts(command: str) -> list[list[str]] | None:
    """The simple commands a shell line is made of — or None if it cannot be
    read with confidence, which the caller must treat as "ask".

    Not a shell parser, and it does not try to be: everything it is unsure of
    is None. Refused outright — substitution, process substitution, heredocs,
    subshells, and output redirected anywhere but /dev/null or another
    descriptor.
    """
    import shlex

    text = command or ""
    if not text.strip():
        return None
    if any(mark in text for mark in ("`", "$(", "<(", ">(", "<<")):
        return None
    try:
        lexer = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:                       # an unbalanced quote
        return None
    parts: list[list[str]] = [[]]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if token in _CONTROL:
            parts.append([])
        elif set(token) <= _OPERATOR_CHARS:
            if token in _DUPLICATES and following is not None and (following.isdigit() or following == "-"):
                index += 1                   # 2>&1 — another descriptor, not a file
            elif token == "<" and following is not None:
                index += 1                   # reads a file
            elif token in _WRITES and following == "/dev/null":
                index += 1
            else:
                return None                  # writes a file, a subshell, `;;`, …
        elif token.isdigit() and following is not None and set(following) <= _OPERATOR_CHARS \
                and (">" in following or "<" in following):
            pass                             # the descriptor number of `2>…`
        else:
            parts[-1].append(token)
        index += 1
    out = []
    for part in parts:
        while part and "=" in part[0] and part[0].split("=", 1)[0].replace("_", "a").isalnum() \
                and not part[0][0].isdigit():
            if part[0].split("=", 1)[0] in _LOADER_VARS:
                return None
            part = part[1:]                  # FOO=bar before the program
        if part:
            out.append(part)
    return out or None


def ssh_target(rest: list[str]) -> tuple[str | None, list[str]]:
    """(host, the words of the remote command) from what follows `ssh`.
    Options that take a value are stepped over with it, so `-p 2244 b200` is
    the host b200 and not the host 2244."""
    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        flag = rest[index]
        index += 2 if (len(flag) == 2 and flag[1] in _SSH_VALUE_FLAGS) else 1
    if index >= len(rest):
        return None, []
    return rest[index], rest[index + 1:]


def shell_rule(argv: list[str]) -> str | None:
    """The kind of command this is, as remembered and as shown — or None when
    it is not a kind that can be remembered."""
    program = argv[0]
    rest = argv[1:]
    if program in NEVER_REMEMBERED:
        return None
    if program == "ssh":
        host, _remote = ssh_target(rest)
        return f"ssh {host}" if host else None
    if program in INTERPRETERS:
        if not rest:
            return None                      # an interactive interpreter
        if rest[0] == "-m" and len(rest) > 1:
            return f"{program} -m {rest[1]}"
        if rest[0].startswith("-"):
            return None                      # -c / -e: inline code is not a kind
        return f"{program} {rest[0]}"
    if program in SUBCOMMAND_TOOLS:
        if not rest:
            return program
        if rest[0].startswith("-"):
            # `git -C repo push`: an option may take a value, so the first
            # bare word is not reliably the subcommand — "git repo" would be
            # remembered, and would match nothing or the wrong thing.
            return None
        return f"{program} {rest[0]}"
    if program == "find" and any(word in _FIND_ACTS for word in rest):
        return None
    return program


def shell_rules(command: str) -> tuple[list[str], str | None]:
    """(kinds to remember, why not all of it can be) for one shell line."""
    parts = shell_parts(command)
    if parts is None:
        return [], ("it uses substitution, a subshell, a heredoc or writes its "
                    "output into a file")
    rules: list[str] = []
    for argv in parts:
        if argv[0] in HARMLESS:
            continue
        rule = shell_rule(argv)
        if rule is None:
            if argv[0] in SUBCOMMAND_TOOLS:
                return [], f"which `{argv[0]}` command this is could not be read past its options"
            return [], f"`{argv[0]}` here runs whatever it is given"
        if rule not in rules:
            rules.append(rule)
    return rules, None


def shell_allowed(command: str, remembered: set[str]) -> bool:
    """True only when EVERY part of the line is a kind already allowed."""
    rules, unreadable = shell_rules(command)
    if unreadable is not None:
        return False
    return bool(rules) and all(rule in remembered for rule in rules)
