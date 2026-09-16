"""Discovering the skills a session can invoke.

The 28 scivo skills are the real command surface — `/paper-writing`,
`/analysis-run`, `/cold-read` — and until now the only way to learn them was to
have read the guide. They are on disk with a name and a one-line description in
their frontmatter, so the completer can simply read them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
FIELD = re.compile(r"^(name|description):\s*(.+?)\s*$", re.M)
FIRST_SENTENCE = re.compile(r"^(.*?[.!?])(?:\s|$)")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str

    @property
    def summary(self) -> str:
        """The first sentence — a completion menu has one line, not five."""
        match = FIRST_SENTENCE.match(self.description)
        return (match.group(1) if match else self.description).strip()


def discover(root: Path) -> list[Skill]:
    """Read `<root>/.claude/skills/*/SKILL.md`, newest convention first.

    Returns [] rather than raising: a missing or malformed skill should cost
    a completion entry, never the session.
    """
    directory = root / ".claude" / "skills"
    if not directory.is_dir():
        return []

    skills: list[Skill] = []
    for entry in sorted(directory.iterdir()):
        manifest = entry / "SKILL.md"
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            continue
        match = FRONTMATTER.match(text)
        fields = dict(FIELD.findall(match.group(1))) if match else {}
        name = fields.get("name") or entry.name
        skills.append(Skill(name=name.strip(), description=fields.get("description", "").strip()))
    return skills
