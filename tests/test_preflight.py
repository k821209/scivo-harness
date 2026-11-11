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


# ── a list that is cut says so ──────────────────────────────────────────────
# The briefing tells the model not to repeat the session-start calls, so a
# list cut in silence is a list it believes it has read whole. Thirty
# decisions came through as twelve, with "re-opening one of these is the most
# expensive thing you can do" under them (review 2026-11-10).

def _section(markdown: str, heading: str) -> str:
    return markdown.split(heading, 1)[1].split("\n## ", 1)[0]


def test_decisions_beyond_the_ones_shown_are_counted_and_pointed_at():
    brief = preflight.Briefing(identity={"project_id": "p"})
    brief.decisions = [{"text": f"settled {i}"} for i in range(30)]
    section = _section(preflight.to_markdown(brief), "## Decisions already settled")
    assert section.count("- settled ") == preflight.DECISIONS_SHOWN
    assert f"{30 - preflight.DECISIONS_SHOWN} older decision(s) NOT shown" in section
    assert "`list_decisions()`" in section


def test_todos_beyond_the_ones_shown_are_counted_and_pointed_at():
    brief = preflight.Briefing(identity={"project_id": "p"})
    brief.todos = [{"text": f"do {i}", "status": "open"} for i in range(40)]
    section = _section(preflight.to_markdown(brief), "## Open to-dos")
    assert section.count("] do ") == preflight.TODOS_SHOWN
    assert f"{40 - preflight.TODOS_SHOWN} more open to-do(s)" in section
    assert "`list_todos()`" in section


def test_a_list_that_fits_claims_nothing_is_missing():
    """The other direction. A note that appears on every briefing is one the
    reader learns to skip, on the one project where it is true."""
    brief = preflight.Briefing(identity={"project_id": "p"})
    brief.decisions = [{"text": f"settled {i}"} for i in range(preflight.DECISIONS_SHOWN)]
    brief.todos = [{"text": f"do {i}", "status": "open"} for i in range(preflight.TODOS_SHOWN)]
    markdown = preflight.to_markdown(brief)
    assert "NOT shown" not in markdown and "not shown" not in markdown


# ── what the briefing repeats, and what it carries for nobody ───────────────
# Measured 2026-11-11 on a real project: one 1.2 KB to-do text three times
# (once under each of two papers as a raw record, once in the to-do list), and
# 6 KB of notes for thirteen servers on a project bound to none of them.

PROMISE = {"name": "open_promises", "label": "Work promised in a to-do and still open",
           "kind": "promise", "ok": False, "stale": [{"text": "x" * 900}],
           "message": "1 to-do(s) open for 3+ days — measure max RSS. Nothing has run since."}


def _two_papers(violations_a, violations_b):
    from scivo_harness.preflight import Briefing, PaperBrief
    brief = Briefing(identity={"project_id": "p"})
    brief.papers = [PaperBrief(slug="a", requirement_violations=violations_a),
                    PaperBrief(slug="b", requirement_violations=violations_b)]
    return brief


def test_a_project_wide_violation_is_written_out_once():
    out = preflight.to_markdown(_two_papers([PROMISE], [PROMISE]))
    assert out.count("measure max RSS") == 1
    assert "the same as under `a`" in out
    # The record behind it is not dumped: the sentence is what the model needs.
    assert "xxxxxxxx" not in out and "'kind'" not in out


def test_a_violation_of_one_paper_is_not_mistaken_for_the_other_papers():
    over = lambda n: {"name": "abstract_words", "label": "Abstract length",  # noqa: E731
                      "message": f"abstract {n}/150 words"}
    out = preflight.to_markdown(_two_papers([over(178)], [over(201)]))
    assert "abstract 178/150 words" in out and "abstract 201/150 words" in out
    assert "the same as under" not in out


def test_a_violation_in_a_shape_nobody_planned_for_is_still_shown():
    out = preflight.to_markdown(_two_papers(["figures 9/6"], [{"limit": 6, "actual": 9}]))
    assert "figures 9/6" in out
    assert "'actual': 9" in out


def _servers(bound):
    from scivo_harness.preflight import Briefing
    brief = Briefing(identity={"project_id": "p"})
    brief.servers = [
        {"alias": "here", "host": "h1", "cores": 8, "notes": "port 2244.\n" + "shared machine. " * 30},
        {"alias": "there", "host": "h2", "cores": 64, "notes": "EBI egress 43 B/s — do not download here.\n" + "more. " * 100},
    ]
    brief.bound_servers = bound
    return brief


def test_a_server_this_project_does_not_use_shows_the_first_line_of_its_notes():
    out = preflight.to_markdown(_servers({"here"}))
    assert "shared machine. shared machine." in out            # bound: the notes
    assert "EBI egress 43 B/s — do not download here." in out  # elsewhere: the first line
    assert "more. more." not in out
    assert "chars total" in out and "get_server(alias)" in out
    assert "on 1 of these 2" in out


def test_a_dataset_on_a_server_counts_as_using_it():
    brief = _servers(set())
    brief.datasets = [{"name": "d", "server_alias": "there", "path": "/x"}]
    assert "more. more." in preflight.to_markdown(brief)


def test_bindings_that_could_not_be_read_hide_nothing():
    """Unknown is not "none": every note is shown, as before."""
    out = preflight.to_markdown(_servers(None))
    assert "shared machine. shared machine." in out and "more. more." in out
    assert "get_server(alias)" not in out


def test_a_long_todo_is_cut_and_says_where_the_rest_is():
    from scivo_harness.preflight import Briefing, TODO_BUDGET, to_markdown
    brief = Briefing(identity={"project_id": "p"})
    brief.todos = [{"status": "open", "text": "Review findings. " + "detail " * 400},
                   {"status": "open", "text": "measure max RSS"}]
    out = to_markdown(brief)
    assert "Review findings." in out and "measure max RSS" in out
    assert out.count("detail") < TODO_BUDGET // 7 + 2
    assert "chars — `list_todos()` for the rest" in out
    assert out.count("list_todos()") == 1          # the short one claims nothing is missing
