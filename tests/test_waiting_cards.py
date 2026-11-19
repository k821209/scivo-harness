"""A card the session waits on is said to be waiting, and one it stops waiting on comes down.

CRE_genemodel, 2026-11-18 and 11-19: the session posted "run … finished —
pick it up?" and waited for the card for nine hours, reading nothing else.
See tests/test_task_wakeup.py for the input side; this is the page's side —
what the head says is waiting, and what becomes of a card nobody will answer.
"""
from __future__ import annotations

import asyncio
import inspect

from scivo_harness import control as C
from tests.test_page_lease import world  # noqa: F401  (the fixture)

CARD = {"questions": [{"question": "run r1 finished, exit 0 — pick it up?", "header": "background task",
                       "options": [{"label": "Pick it up"}, {"label": "Not now"}]}]}


def test_the_head_lists_the_cards_that_are_waiting_oldest_first(world):
    _store, boot = world

    async def main():
        ctl = await boot()
        assert ctl._waiting() == []
        approval = asyncio.create_task(ctl.ask_approval("Bash", "ls", False))
        await asyncio.sleep(0)
        ctl.status("something in between")
        question = asyncio.create_task(ctl.ask_question(CARD))
        await asyncio.sleep(0)
        waiting = ctl._waiting()
        await ctl._write_head(state="live")
        on_head = _store.docs["content"]["head"]["waiting"]
        # Answer the approval; the question still waits.
        next(iter(ctl._approvals.values())).future.set_result("allow")
        # Answered, though the coroutine that asked has not run on yet: it is
        # not waiting any more, whatever the bookkeeping still holds.
        assert [card["kind"] for card in ctl._waiting()] == ["question"]
        await approval
        after_one = ctl._waiting()
        next(iter(ctl._questions_pending.values())).future.set_result({"q": "Not now"})
        await question
        return ctl, waiting, on_head, after_one
    ctl, waiting, on_head, after_one = asyncio.run(main())
    assert [card["kind"] for card in waiting] == ["approval", "question"]
    assert [ctl.events[card["i"]]["kind"] for card in waiting] == ["approval", "question"]
    assert waiting[0]["i"] < waiting[1]["i"]
    assert on_head == waiting, "the page reads it from the head"
    assert [card["kind"] for card in after_one] == ["question"]
    assert ctl._waiting() == []


def test_the_head_is_rewritten_when_a_card_goes_up_or_comes_down(world):
    """Not on the heartbeat alone: an idle session beats every five minutes,
    and a bar saying "waiting for you" five minutes after the answer is a
    page that is wrong about the one thing it is there to say."""
    _store, boot = world

    async def main():
        ctl = await boot()
        await ctl._write_head(state="live")
        assert ctl._waiting() == ctl._head_waiting == []
        task = asyncio.create_task(ctl.ask_question(CARD))
        await asyncio.sleep(0)
        up = ctl._waiting() != ctl._head_waiting
        await ctl._write_head(state="live")
        settled = ctl._waiting() == ctl._head_waiting
        next(iter(ctl._questions_pending.values())).future.set_result(None)
        await task
        down = ctl._waiting() != ctl._head_waiting
        return up, settled, down
    assert asyncio.run(main()) == (True, True, True)
    assert "self._waiting() != getattr(self, \"_head_waiting\", [])" in inspect.getsource(C.Control._writer)


def test_a_card_nobody_is_waiting_on_any_more_comes_down_and_says_why(world):
    _store, boot = world

    async def main():
        ctl = await boot()
        task = asyncio.create_task(ctl.ask_question(CARD, set_aside="set aside — a message was typed instead"))
        await asyncio.sleep(0)
        index = ctl._waiting()[0]["i"]
        assert ctl.events[index]["state"] == "pending"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return ctl, index, task
    ctl, index, task = asyncio.run(main())
    assert task.cancelled()
    event = ctl.events[index]
    assert event["state"] == "skipped" and event["answers"] is None
    assert event["note"] == "set aside — a message was typed instead"
    assert ctl._waiting() == [] and ctl._questions_pending == {}
    assert index // C.CHUNK in ctl._dirty, "and the page is sent the change"


def test_an_answered_card_carries_no_note(world):
    _store, boot = world

    async def main():
        ctl = await boot()
        task = asyncio.create_task(ctl.ask_question(CARD, set_aside="unused"))
        await asyncio.sleep(0)
        index = ctl._waiting()[0]["i"]
        next(iter(ctl._questions_pending.values())).future.set_result({"q": "Pick it up"})
        return ctl, index, await task
    ctl, index, answers = asyncio.run(main())
    assert answers == {"q": "Pick it up"}
    assert ctl.events[index]["state"] == "answered" and "note" not in ctl.events[index]
