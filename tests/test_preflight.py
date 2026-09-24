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
