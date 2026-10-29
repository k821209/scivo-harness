"""The briefing tells the model what the server said, including the notes."""
from __future__ import annotations

from scivo_harness import preflight, sessions


def test_the_briefing_carries_the_key_scope_and_install_notes():
    brief = preflight.Briefing()
    brief.identity = {"project_id": "p", "guide_version": "g",
                      "key_scope": "account", "key_scope_note": "reaches the whole account",
                      "install_note": "runs its own copy, nothing to restore"}
    text = preflight.to_markdown(brief)
    assert "reaches the whole account" in text
    assert "nothing to restore" in text


def test_transcript_discovery_honours_the_relocated_config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"; proj = cfg / "projects" / "-home-u-works-x"; proj.mkdir(parents=True)
    (proj / "abc123.jsonl").write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert sessions.transcript_path("abc123") == proj / "abc123.jsonl"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    assert sessions.transcript_path("abc123") is None


# ── questions on study documents ────────────────────────────────────────────
# A question a reader left on a study is the one kind of waiting work that
# lives nowhere near a paper, so nothing else in this briefing would show it
# (user, 2026-10-29). Split by who asked: a reader's question waits on this
# session, the session's own waits on the reader.

def test_a_readers_question_reaches_the_briefing():
    from scivo_harness.preflight import Briefing, to_markdown
    brief = Briefing(identity={"project_id": "p", "project_name": "P"})
    brief.study_questions = {"from_reader": 2, "from_agent": 0, "total": 2}
    out = to_markdown(brief)
    assert "## Questions on study documents" in out
    assert "2 question(s) a reader left on a study" in out
    assert "answer_study_question" in out
    assert "changed_study" in out


def test_the_sessions_own_question_is_not_handed_back_to_it_to_answer():
    from scivo_harness.preflight import Briefing, to_markdown
    brief = Briefing(identity={"project_id": "p"})
    brief.study_questions = {"from_reader": 0, "from_agent": 1, "total": 1}
    out = to_markdown(brief)
    assert "left for the\nreader" in out or "left for the reader" in out
    assert "do not answer your own" in out


def test_no_heading_when_nothing_is_waiting():
    """A "0 questions" heading on every project trains the reader past the one
    session it matters."""
    from scivo_harness.preflight import Briefing, to_markdown
    brief = Briefing(identity={"project_id": "p"})
    brief.study_questions = {"from_reader": 0, "from_agent": 0, "total": 0}
    assert "Questions on study documents" not in to_markdown(brief)
    brief.study_questions = {}
    assert "Questions on study documents" not in to_markdown(brief)
