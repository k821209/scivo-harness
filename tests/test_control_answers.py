"""How a page answer travels back, and why not keyed by the question.

The control page writes the owner's answer to Firestore as a map, so the map's
keys become part of a property path — and a property path may not exceed 1500
bytes. Keyed by question TEXT, a long question made every click fail with

    Not sent: property path is longer than 1500 bytes

The card rolled back correctly on the failure, so from the outside the button
simply did nothing: 22 finished background tasks in one question, and no way to
answer it (user, 2026-11-03). Shortening the question is a mitigation that
breaks again at 1500 bytes; the fix is not to put the text in the key.

`AskUserQuestion` does want answers keyed by question text, so the harness maps
indices back using the questions it posted.
"""
from __future__ import annotations

import asyncio
import pathlib
import types

from scivo_harness.control import Control, _Question

_PAGE_HTML = (pathlib.Path(__file__).resolve().parents[1]
              / "src" / "scivo_harness" / "control_page.html")


def _row(extra: dict) -> dict:
    """A row as `list_responses` hands it over: the router drops anything not
    written by the owner for this session."""
    from scivo_harness.control import OWNER

    return {"doc": "questions", "reviewer": OWNER, "sid": "s1", **extra}


def _control_with(questions: list[dict]) -> tuple[Control, _Question]:
    control = Control.__new__(Control)          # no network, no Firestore
    control.events = [{"kind": "question", "rid": "r1", "questions": questions}]
    return control, _Question("r1", 0, asyncio.Future())


def test_indices_come_back_as_the_question_text():
    long_question = "task b1 completed; " * 200          # well past 1500 bytes
    control, pending = _control_with(
        [{"question": long_question}, {"question": "second?"}])
    named = control._name_answers(pending, {"0": "Pick it up", "1": "Not now"})
    assert named == {long_question: "Pick it up", "second?": "Not now"}
    # The thing the transport must never carry as a key.
    assert len(long_question.encode()) > 1500


def test_a_page_loaded_before_this_change_still_gets_its_answer_through():
    # An open browser tab still sends question text. An answer that arrives is
    # worth more than a tidy shape.
    control, pending = _control_with([{"question": "which?"}])
    assert control._name_answers(pending, {"which?": "A"}) == {"which?": "A"}


def test_an_index_with_no_question_behind_it_does_not_crash():
    control, pending = _control_with([{"question": "only one"}])
    assert control._name_answers(pending, {"7": "A"}) == {"7": "A"}


def test_answers_survive_an_event_with_no_questions_on_it():
    control = Control.__new__(Control)
    control.events = [{"kind": "question", "rid": "r1"}]
    pending = _Question("r1", 0, asyncio.Future())
    assert control._name_answers(pending, {"0": "A"}) == {"0": "A"}


def test_the_page_never_uses_the_question_text_as_a_draft_key():
    """A static check, because the page's JavaScript is not exercised here —
    and this one line is what made the button dead."""
    html = _PAGE_HTML.read_text(encoding="utf-8")
    assert "draft[String(qi)] = value;" in html
    assert "draft[q.question]" not in html, "the question text is back in the key"
    assert "draft[String(i)] !== undefined" in html, "the completeness check too"


def test_the_router_hands_the_tool_text_keyed_answers():
    """Through `_intake`, the path the page actually goes through.

    Found by falsifying this file: unwiring `_name_answers` from the router
    broke no test, because every test called it directly. A mapping nothing
    drives is a mapping that can be removed by accident.
    """
    long_question = "task b1 completed; " * 200

    async def drive():
        # The future has to be made under the running loop.
        control, pending = _control_with(
            [{"question": long_question}, {"question": "second?"}])
        control._questions_pending = {"r1": pending}
        control._approvals = {}
        control._interrupt_seen = 0
        control.sid = "s1"
        await control._intake([_row({"answers": {"r1": {"0": "Pick it up", "1": "Not now"}}})])
        return pending.future.result()

    assert asyncio.run(drive()) == {long_question: "Pick it up", "second?": "Not now"}


def test_the_router_treats_an_empty_answer_as_a_skip():
    async def drive():
        control, pending = _control_with([{"question": "which?"}])
        control._questions_pending = {"r1": pending}
        control._approvals = {}
        control._interrupt_seen = 0
        control.sid = "s1"
        await control._intake([_row({"answers": {"r1": None}})])
        return pending.future.result()

    assert asyncio.run(drive()) is None
