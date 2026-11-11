"""One session drives a project's page at a time, and a second one finds out.

A project has one control page. A second session in the same project used to
start on it anyway: it deleted the first one's transcript as "previous
sessions" — the first was running — and the two then took turns writing the
head, so the page showed one conversation, then the other, every few seconds
(review 2026-11-10, reproduced).
"""
from __future__ import annotations

import asyncio
import os
import socket

import pytest

from scivo_harness import control as C
from scivo_harness.config import ScivoConfig
from scivo_harness.scivo_mcp import ToolOutcome


class Firestore:
    """One project's publication, shared by every session in the test."""

    def __init__(self):
        self.docs: dict[str, dict[str, dict]] = {}
        self.writes: list[tuple[str, str]] = []

    def client(self):
        store = self

        class Client:
            async def call(self, name, **kw):
                return store.call(name, **kw)
        return Client()

    def call(self, name, **kw):
        if name == "publish_page":
            return ToolOutcome(True, items=[{"pub_id": "pub1", "url": "https://x/p"}])
        if name == "put_page_data":
            self.docs.setdefault(kw["collection"], {})[kw["doc_id"]] = dict(kw["data"])
            self.writes.append((kw["collection"], kw["doc_id"]))
        elif name == "clear_page_data":
            if kw.get("doc_id"):
                self.docs.get(kw["collection"], {}).pop(kw["doc_id"], None)
            else:
                self.docs[kw["collection"]] = {}
        elif name == "list_responses":
            rows = self.docs.get(kw.get("collection", "responses"), {})
            return ToolOutcome(True, items=[{"id": k, **v} for k, v in rows.items()])
        return ToolOutcome(True, items=[{}])


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "LEASE_ANSWER_WAIT", 1.0)
    store = Firestore()

    async def boot(take=False):
        """What `start()` does up to the point of spawning its loops."""
        ctl = C.Control(ScivoConfig(root=tmp_path, command="x"), "proj", "pid", "m")
        ctl._client = store.client()
        ctl.sid = f"s-{len(store.writes)}-{os.urandom(2).hex()}"
        ctl.link = await ctl._ensure_publication(take=take)
        await ctl._write_lease()
        # Every session in a test is this one process. A real second session
        # is ANOTHER live process on the machine, so the lease is made to say
        # so (the parent: certainly alive, certainly not us).
        store.docs["lease"]["owner"]["pid"] = os.getppid()
        await ctl._write_head(state="live")
        return ctl
    return store, boot


def test_a_second_session_leaves_a_live_page_alone(world):
    store, boot = world

    async def main():
        first = await boot()
        first.user("the first terminal's conversation", "terminal")
        await first._flush()
        before = {k: dict(v) for k, v in store.docs["content"].items()}
        with pytest.raises(C.PageInUse) as refused:
            await boot()
        return first, before, str(refused.value)
    first, before, said = asyncio.run(main())
    # Nothing of the first session was touched: not its transcript, not its head.
    assert store.docs["content"] == before
    assert store.docs["content"]["head"]["sid"] == first.sid
    assert first.sid in said and "/scivo-control take" in said
    assert "on this machine" in said               # WHY it believes it is alive


def test_take_moves_the_page_and_tells_the_session_that_had_it(world, capsys):
    store, boot = world

    async def main():
        first = await boot()
        second = await boot(take=True)
        assert store.docs["content"]["head"]["sid"] == second.sid
        assert store.docs["lease"]["owner"]["sid"] == second.sid
        # The first reads its inbox, as it would on its listener:
        acted = await first._intake([{"id": k, **v} for k, v in store.docs["responses"].items()])
        await asyncio.sleep(0.05)                  # the yield runs as its own task
        return first, second, acted
    first, second, acted = asyncio.run(main())
    assert acted == 1
    assert first.taken_by == second.sid
    assert not first.active, "it must stop writing to a page that is someone else's"
    assert "took the web page" in capsys.readouterr().out
    assert "took the page over" in second._claim_note


def test_a_session_that_yielded_does_not_write_the_head_again(world):
    store, boot = world

    async def main():
        first = await boot()
        second = await boot(take=True)
        await first._intake([{"id": k, **v} for k, v in store.docs["responses"].items()])
        await asyncio.sleep(0.05)
        first.user("typed in the first terminal afterwards", "terminal")
        first.status("still working")
        assert not first._tasks
        return second
    second = asyncio.run(main())
    assert store.docs["content"]["head"]["sid"] == second.sid


def test_yielding_does_not_end_or_unpublish_the_page(world):
    """`stop()` marks the page ended and unpublishes it — which, done by the
    session that LOST the page, would shut down someone else's."""
    store, boot = world
    calls = []
    original = store.call
    store.call = lambda name, **kw: (calls.append(name), original(name, **kw))[1]

    async def main():
        first = await boot()
        await boot(take=True)
        calls.clear()
        await first._yield_page("the-other")
    asyncio.run(main())
    assert "update_publication" not in calls
    assert store.docs["content"]["head"]["state"] == "live"


def test_a_holder_whose_process_is_gone_is_replaced_without_asking(world, monkeypatch):
    store, boot = world

    async def main():
        first = await boot()
        store.docs["lease"]["owner"]["pid"] = 2_000_000_000        # nobody
        monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
        second = await boot()
        return first, second
    first, second = asyncio.run(main())
    assert store.docs["lease"]["owner"]["sid"] == second.sid
    assert "process" in second._claim_note and "gone" in second._claim_note


def test_a_lease_long_past_its_renewal_is_nobodys(world):
    store, boot = world

    async def main():
        await boot()
        store.docs["lease"]["owner"]["at"] -= int(C.LEASE_EVERY * 2.5 * 1000) + 5000
        return await boot()
    second = asyncio.run(main())
    assert "ran out" in second._claim_note


def test_a_holder_on_another_machine_is_asked_and_one_that_answers_is_left_alone(world, monkeypatch):
    store, boot = world

    async def main():
        first = await boot()
        store.docs["lease"]["owner"]["host"] = "another-machine"

        async def holder_answers():
            # what the live holder does when the mark reaches it: renew
            for _ in range(40):
                await asyncio.sleep(0.05)
                if "ping" in store.docs.get("responses", {}):
                    store.docs["lease"]["owner"]["at"] += 1
                    return
        answering = asyncio.create_task(holder_answers())
        with pytest.raises(C.PageInUse) as refused:
            await boot()
        await answering
        return first, str(refused.value)
    first, said = asyncio.run(main())
    assert "it answered" in said and "another-machine" in said
    ping = store.docs["responses"]["ping"]
    assert ping["sid"] == first.sid and ping["reviewer"] == "owner" and ping["doc"] == "control"


def test_asking_does_not_wipe_the_holders_stop_counter(world):
    """The question is its own document. Written over `control`, it replaced
    the counter a reloaded tab resumes from, and Stop went dead."""
    store, boot = world

    async def main():
        await boot()
        store.docs["lease"]["owner"]["host"] = "another-machine"
        store.docs.setdefault("responses", {})["control"] = {"doc": "control", "interrupt": 3}
        with pytest.raises(C.PageInUse):
            await boot()
    asyncio.run(main())
    assert store.docs["responses"]["control"] == {"doc": "control", "interrupt": 3}


def test_a_listening_holder_elsewhere_that_stays_silent_is_gone(world):
    store, boot = world

    async def main():
        await boot()
        store.docs["lease"]["owner"].update(host="another-machine", listening=True)
        return await boot()
    second = asyncio.run(main())
    assert "did not answer" in second._claim_note


def test_a_silent_holder_that_only_reads_on_a_timer_gets_the_benefit_of_the_doubt(world):
    """It may not have looked yet. Deleting a live conversation is the worse
    mistake, and `take` is one word."""
    store, boot = world

    async def main():
        await boot()
        store.docs["lease"]["owner"].update(host="another-machine", listening=False)
        with pytest.raises(C.PageInUse) as refused:
            await boot()
        return str(refused.value)
    said = asyncio.run(main())
    assert "may still be there" in said and "/scivo-control take" in said


def test_a_viewer_mark_makes_the_holder_renew_at_once(world):
    """That is the answer to "are you there?" — and the same mark a person
    opening the page writes."""
    store, boot = world

    async def main():
        first = await boot()
        first._lease_due = False
        await first._intake([{"id": "ping", "doc": "control", "sid": first.sid,
                              "reviewer": "owner", "viewer": 123}])
        return first
    first = asyncio.run(main())
    assert first._lease_due is True


def test_the_lease_is_not_rewritten_with_every_beat(world, monkeypatch):
    store, boot = world
    monkeypatch.setattr(C, "BEAT_EVERY", 0.02)
    monkeypatch.setattr(C, "BEAT_IDLE", 0.02)

    async def main():
        first = await boot()
        store.writes.clear()
        task = asyncio.create_task(first._writer())
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(main())
    heads = sum(1 for c, d in store.writes if (c, d) == ("content", "head"))
    leases = sum(1 for c, d in store.writes if c == "lease")
    assert heads >= 5 and leases == 0, (heads, leases)


def test_stopping_gives_the_lease_up_so_the_next_start_is_not_made_to_wait(world):
    store, boot = world

    async def main():
        first = await boot()
        await first.stop()
        return await boot()                        # e.g. the same session after /update
    second = asyncio.run(main())
    assert store.docs["lease"]["owner"]["sid"] == second.sid
    assert second._claim_note == ""


def test_a_page_with_no_lease_is_started_on_as_before(world):
    """A session from before leases wrote none. It cannot be seen, so it
    cannot be spared — the old behaviour, for the old sessions only."""
    store, boot = world
    store.docs["content"] = {"head": {"sid": "an-old-session", "beat": 1}}
    (asyncio.run(boot()))
    assert store.docs["lease"]["owner"]["sid"] != "an-old-session"


def test_a_start_that_is_refused_leaves_nothing_looking_started(tmp_path, monkeypatch):
    store = Firestore()
    store.docs["lease"] = {"owner": {"sid": "holder", "at": C._now_ms(), "every": 60,
                                     "host": socket.gethostname(), "pid": os.getppid()}}
    (tmp_path / ".scivo").mkdir()
    (tmp_path / ".scivo" / "control.json").write_text('{"pub_id": "pub1", "url": "https://x/p"}')
    closed = []

    class Stack:
        async def enter_async_context(self, _cm):
            return store.client()

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(C, "AsyncExitStack", Stack)
    monkeypatch.setattr(C, "connect", lambda config: None)
    ctl = C.Control(ScivoConfig(root=tmp_path, command="x"), "proj", "pid", "m")
    with pytest.raises(C.PageInUse):
        asyncio.run(ctl.start())
    assert not ctl.active and closed == [True]
    assert "head" not in store.docs.get("content", {})


def test_a_process_that_re_executed_itself_is_not_locked_out_by_its_own_leftover(world):
    """`/update` execs: same pid, new process. It gives the lease up on the
    way out — and if that one write was lost, "process N is alive on this
    machine" would be this session refusing itself its own page."""
    store, boot = world

    async def main():
        await boot()
        store.docs["lease"]["owner"]["pid"] = os.getpid()
        return await boot()
    second = asyncio.run(main())
    assert "this process before it restarted" in second._claim_note
