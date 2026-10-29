"""What `scivo -c` shows of the conversation it is continuing.

The recap used to put each turn through `" ".join(text.split())` and cut it at
240 characters. That collapses every paragraph, list and code block into one
dim run-on line and cuts mid-sentence, so a resumed conversation came back
looking like a log file instead of like the conversation it was
(user, 2026-10-29 — "뭉쳐져서 구림"). It is printed the way the live session
prints it now.
"""
from __future__ import annotations

import json

from scivo_harness import sessions
from scivo_harness.repl import ANSI, RECAP_LINES, recap_block


def plain(lines: list[str]) -> list[str]:
    return [ANSI.sub("", line) for line in lines]


def test_a_typed_message_comes_back_at_the_prompt_it_was_typed_at():
    block = plain(recap_block("user", "피드백 확인\n그리고 배포까지"))
    assert block == ["scivo> 피드백 확인",
                     "       그리고 배포까지"]
    # Lines after the first sit under the typed text, not under the prompt.
    assert block[1].index("그리고") == len("scivo> ")


def test_a_reply_keeps_its_paragraphs_and_its_left_margin():
    text = "Two things:\n\n- the probe\n- the profile\n\nBoth are committed."
    block = plain(recap_block("assistant", text))
    assert block == ["Two things:", "", "- the probe", "- the profile", "",
                     "Both are committed."]
    # Flush left, like the live stream: an indent would make a reply read as
    # quoted log output.
    assert not any(line.startswith(" ") for line in block if line)


def test_too_long_is_cut_by_lines_and_says_how_many_were_left():
    text = "\n".join(f"line {n}" for n in range(1, 31))
    block = plain(recap_block("assistant", text))
    assert block[:3] == ["line 1", "line 2", "line 3"]
    assert len(block) == RECAP_LINES + 1
    assert block[RECAP_LINES - 1] == f"line {RECAP_LINES}"
    assert block[-1].strip() == f"… {30 - RECAP_LINES} more lines"
    # And no line is cut in half to make room.
    assert all(line == "" or line.startswith("line ") or "more lines" in line
               for line in block)


def test_one_dropped_line_is_singular():
    text = "\n".join(f"line {n}" for n in range(RECAP_LINES + 1))
    assert plain(recap_block("assistant", text))[-1].strip() == "… 1 more line"


def test_a_short_message_gains_nothing():
    assert plain(recap_block("assistant", "done.")) == ["done."]


def _transcript(tmp_path, entries):
    path = tmp_path / "abcd1234.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def test_two_entries_from_one_speaker_are_not_glued_into_one_paragraph(tmp_path, monkeypatch):
    """Text, a tool call, more text is three transcript entries and one
    speaker. The tool lines are not shown, so joining the halves with a single
    newline invents a paragraph that was never on the screen."""
    path = _transcript(tmp_path, [
        {"type": "user", "message": {"content": "run the gates"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Checking."}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "13/13."}]}},
    ])
    monkeypatch.setattr(sessions, "transcript_path", lambda _id: path)
    turns = sessions.recap("abcd1234")
    assert [role for role, _ in turns] == ["user", "assistant"]
    assert turns[1][1] == "Checking.\n\n13/13."
    assert plain(recap_block("assistant", turns[1][1])) == ["Checking.", "", "13/13."]
