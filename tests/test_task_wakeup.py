"""What a finished background task does while the prompt sits idle.

The September pump made between-turn notifications VISIBLE — `_on_task_note`
prints them the moment the CLI delivers them. It did not make them acted on: a
turn begins only when `_next_input` returns a line, so the model stayed asleep
until the user typed something (user, 2026-11-03: "이런 메세지가 와도 scivo
harness 는 내가 확인이라고 하기 전까지 응답을 안하네?"). The notification is
already in the conversation by then — that is why typing anything at all
produced a report — so waking up needs a turn, not the text.

Three modes, and `ask` is the default by the user's own call ("디폴트는
물어보는걸로하고 항상깨우는 설정을 만드는걸로"): spending a turn, with the tool
calls that follow, while nobody is watching should be chosen rather than
inherited. `always` is the setting for when it was chosen.

The delicate part is the race in `_next_input`: the notification is a third
waiter beside the terminal prompt and the control page, and cancelling a
prompt_toolkit prompt drops whatever was half-typed into it.
"""
from __future__ import annotations

import asyncio
import types

from scivo_harness.repl import ANSI, Repl


class FakeLine:
    """A prompt that answers in sequence; `None` hangs forever.

    `_offer_wake` asks a SECOND time (the offer), so a race test needs the
    first call to hang — letting the wake win — and the second to answer.
    """

    def __init__(self, answers=(), rich: bool = True, typed: str = "", on_ask=None):
        self.answers = list(answers)
        self.rich = rich
        self.typed = typed
        self.on_ask = on_ask
        self.asked = 0
        self.defaults: list[str] = []
        self.prompts: list[str] = []

    async def ask(self, styled, plain, default=""):
        self.asked += 1
        self.defaults.append(default)
        self.prompts.append(ANSI.sub("", styled))
        if self.on_ask is not None and self.asked == 1:
            # A notification landing while the prompt is OPEN: the race,
            # rather than one that was already waiting.
            self.on_ask()
        answer = self.answers.pop(0) if self.answers else None
        if answer is None:
            await asyncio.Event().wait()
        return answer

    def pending_text(self) -> str:
        return self.typed


def _repl(line: FakeLine | None = None, notify: str = "ask"):
    stub = types.SimpleNamespace(
        line=line or FakeLine(),
        control=None,
        mode="default",
        _typed_ahead="",
        session_id="s-this-session",
        notify_mode=notify,
        _wake=asyncio.Event(),
        _wake_notes=[],
        said=[],
    )
    stub._say = stub.said.append
    for name in ("_next_input", "_on_task_note", "_consume_wake", "_wake_line",
                 "_announce_wake", "_offer_wake", "_offer_on_page", "_notify"):
        setattr(stub, name, getattr(Repl, name).__get__(stub))
    # The card's labels are read back off the answer, so the stub takes them
    # from the class rather than repeating the strings.
    stub._PICK_UP, stub._NOT_NOW = Repl._PICK_UP, Repl._NOT_NOW
    return stub


def _note(stub, task="b51wc35va", status="completed", summary="", session=..., output=""):
    """A notification as the CLI delivers it — including whose task it was.

    `session` defaults to this session's own id; pass another to post a
    notification that belongs somewhere else.
    """
    stub._on_task_note(types.SimpleNamespace(
        task_id=task, status=status, summary=summary, output_file=output,
        session_id=stub.session_id if session is ... else session))


def _input(stub, timeout: float = 2.0):
    """A wake-up that never arrives must FAIL, not hang.

    Found the hard way while falsifying these tests: with the wake removed the
    fake prompt waits forever, so pytest hung instead of reporting. A suite
    that hangs is worse than one that fails — it stalls whoever is waiting on
    it and says nothing about why.
    """

    async def go():
        return await asyncio.wait_for(stub._next_input(), timeout)

    return asyncio.run(go())


# ── the default: ask ───────────────────────────────────────────────────────

def test_the_default_asks_before_spending_a_turn():
    stub = _repl(FakeLine(answers=["y"]))
    _note(stub)
    line, via = _input(stub)
    assert via == "task"
    assert "b51wc35va" in line and "completed" in line
    # Marked as not typed by the user, so the transcript cannot read as if
    # they had asked.
    assert line.startswith("[scivo]")
    assert "pick it up?" in stub.line.prompts[-1]


def test_declining_the_offer_starts_no_turn():
    stub = _repl(FakeLine(answers=[""]))
    _note(stub)
    assert _input(stub) == ("", "terminal")       # the REPL skips an empty line
    assert not stub._wake.is_set() and stub._wake_notes == []


def test_words_instead_of_yes_become_the_turn():
    stub = _repl(FakeLine(answers=["로그 먼저 보여줘"]))
    _note(stub)
    # Their own words carry the notification anyway — it is in the
    # conversation — so there is no reason to make them answer y/n first.
    assert _input(stub) == ("로그 먼저 보여줘", "terminal")


def test_the_word_the_user_was_typing_by_hand_counts_as_yes():
    for word in ("확인", "ㄱㄱ", "Y", "ok"):
        stub = _repl(FakeLine(answers=[word]))
        _note(stub)
        assert _input(stub)[1] == "task", word


def test_five_notifications_are_one_offer_that_counts_the_rest():
    stub = _repl(FakeLine(answers=["y"]))
    for n in range(5):
        _note(stub, task=f"task{n}")
    line, via = _input(stub)
    assert via == "task" and stub.line.asked == 1, "one offer, not five"
    assert "5 background tasks finished" in line
    assert "task0" in line and "(+3 more above)" in line
    assert not stub._wake.is_set() and stub._wake_notes == []


def test_the_wake_line_stays_a_line():
    """Five finished tasks with summaries and output paths made a
    700-character "line", and the dispatch loop handed it to
    `Path(...).exists()`: `OSError: [Errno 36] File name too long`, raised
    where nothing catches it, so the session died. Two ends were fixed — this
    line is bounded, and `image_path_in` no longer raises on anything — and
    either alone would have stopped the crash. Both, because a bounded line
    is not a guarantee about the next caller and a hardened probe is not a
    reason to inject prose as a line."""
    from scivo_harness.attachments import image_path_in

    stub = _repl(FakeLine(answers=["y"]))
    for n in range(5):
        _note(stub, task=f"task{n}", summary="Run the remote self-report script "
                                             "with stderr visible" * 3,
              output=f"/tmp/claude-1001/-home-k821209-works/session/tasks/task{n}.output")
    line, _ = _input(stub)
    assert len(line) < 800, len(line)
    # And the call that crashed answers instead of raising.
    assert image_path_in(line) is None


def test_the_offer_carries_a_half_typed_line_as_its_default():
    # The prompt is open with half a line in it when the task finishes.
    stub = _repl()
    stub.line = FakeLine(answers=[None, "y"], typed="그 로그 좀 ",
                         on_ask=lambda: _note(stub))
    assert _input(stub)[1] == "task"
    # Read before the prompt was cancelled and handed to the offer, rather
    # than lost with the cancelled buffer.
    assert stub.line.defaults[1] == "그 로그 좀 "


def test_the_offer_does_not_send_typed_ahead_twice():
    # Typed while the model was replying, then a task finishes before the
    # prompt is drawn: the offer's prompt carries it, so it must not ALSO be
    # left pending for the next prompt.
    stub = _repl(FakeLine(answers=["y"]))
    stub._typed_ahead = "그리고 배포까지"
    _note(stub)
    assert _input(stub)[1] == "task"
    assert stub.line.defaults[0] == "그리고 배포까지"
    assert stub._typed_ahead == ""


# ── the setting: always ────────────────────────────────────────────────────

def test_always_starts_the_turn_with_nobody_asked():
    stub = _repl(FakeLine(answers=[]), notify="always")   # a prompt that hangs
    _note(stub)
    line, via = _input(stub)
    assert via == "task" and stub.line.asked == 0, "nobody should have been asked"
    assert "b51wc35va" in line


def test_always_keeps_what_was_typed_during_the_last_turn():
    # No prompt is drawn, so text from the KeyWatcher is still unsent.
    stub = _repl(FakeLine(answers=[]), notify="always")
    stub._typed_ahead = "그리고 배포까지"
    _note(stub)
    assert _input(stub)[1] == "task"
    assert stub._typed_ahead == "그리고 배포까지"


# ── off, and the modes themselves ─────────────────────────────────────────

def test_off_prints_and_waits():
    stub = _repl(FakeLine(answers=["ok"]), notify="off")
    _note(stub)
    assert not stub._wake.is_set(), "off must not queue a wake-up"
    assert _input(stub) == ("ok", "terminal")


def test_switching_to_off_drops_a_wake_already_queued():
    stub = _repl(FakeLine(answers=["ok"]))
    _note(stub)
    assert stub._wake.is_set()
    stub._notify("off")
    assert not stub._wake.is_set() and stub.notify_mode == "off"
    assert _input(stub) == ("ok", "terminal")
    assert "notify   off" in ANSI.sub("", "".join(stub.said))


def test_notify_rejects_a_mode_it_does_not_have():
    stub = _repl()
    stub._notify("sometimes")
    assert stub.notify_mode == "ask"
    assert "takes off | ask | always" in ANSI.sub("", "".join(stub.said))


def test_the_env_default_is_ask_and_a_bad_value_is_not_fatal(monkeypatch):
    from scivo_harness.repl import _notify_mode_from_env

    monkeypatch.delenv("SCIVO_NOTIFY", raising=False)
    assert _notify_mode_from_env() == "ask"
    monkeypatch.setenv("SCIVO_NOTIFY", "ALWAYS")
    assert _notify_mode_from_env() == "always"
    monkeypatch.setenv("SCIVO_NOTIFY", "yes please")
    assert _notify_mode_from_env() == "ask", "a typo must not decide this loudly"


def test_a_dumb_prompt_is_offered_before_it_opens_but_cannot_be_interrupted():
    # SCIVO_PROMPT_STYLE=simple. Already waiting when we arrive: offered.
    early = _repl(FakeLine(answers=["y"], rich=False))
    _note(early)
    assert _input(early)[1] == "task"
    # Arriving while the thread is reading stdin: it cannot be withdrawn, so
    # the typed line wins and carries the notification into that turn.
    during = _repl()
    during.line = FakeLine(answers=["ok"], rich=False, on_ask=lambda: _note(during))
    assert _input(during) == ("ok", "terminal")
    assert not during._wake.is_set()


# ── on the scivo-control page ─────────────────────────────────────────────

class FakePage:
    """The control page: a question card, and whatever the owner picked."""

    def __init__(self, answer=None, active: bool = True):
        self.active = active
        self.answer = answer
        self.asked: list[dict] = []
        self.statuses: list[str] = []
        self.messages: asyncio.Queue = asyncio.Queue()
        self.waiting = False

    def status(self, text, level="info"):
        self.statuses.append(text)

    async def ask_question(self, payload):
        self.asked.append(payload)
        question = payload["questions"][0]["question"]
        return None if self.answer is None else {question: self.answer}


def _on_page(answer=None, notify="ask", line: FakeLine | None = None):
    stub = _repl(line or FakeLine(answers=[]), notify=notify)   # terminal hangs
    stub.control = FakePage(answer)
    return stub


def test_the_offer_is_a_card_on_the_page_not_a_prompt_in_the_terminal():
    # The mistake permissions._questions already records: the page exists and
    # is never called, so whoever is on the page has to walk to the terminal.
    stub = _on_page(answer=Repl._PICK_UP)
    _note(stub)
    line, via = _input(stub)
    assert via == "task" and "b51wc35va" in line
    assert len(stub.control.asked) == 1, "the page was never asked"
    assert stub.line.asked == 0, "it asked in the terminal while the page was up"
    card = stub.control.asked[0]["questions"][0]
    assert "pick it up?" in card["question"]
    assert [o["label"] for o in card["options"]] == ["Pick it up", "Not now"]


def test_not_now_on_the_page_starts_no_turn():
    stub = _on_page(answer=Repl._NOT_NOW)
    _note(stub)
    assert _input(stub) == ("", "terminal")
    assert not stub._wake.is_set() and stub._wake_notes == []


def test_a_skipped_card_starts_no_turn():
    stub = _on_page(answer=None)          # the page skipped it
    _note(stub)
    assert _input(stub) == ("", "terminal")


def test_words_typed_into_the_card_become_the_turn():
    stub = _on_page(answer="로그 먼저 보여줘")
    _note(stub)
    assert _input(stub) == ("로그 먼저 보여줘", "terminal")


def test_a_multi_select_answer_is_read_as_its_first_label():
    stub = _on_page(answer=[Repl._PICK_UP])
    _note(stub)
    assert _input(stub)[1] == "task"


def test_the_page_offer_keeps_what_was_typed_in_the_terminal():
    # No terminal prompt is drawn while the card is up, so text typed during
    # the last turn must stay pending rather than vanish with the offer.
    stub = _on_page(answer=Repl._NOT_NOW)
    stub._typed_ahead = "그리고 배포까지"
    _note(stub)
    _input(stub)
    assert stub._typed_ahead == "그리고 배포까지"


# ── whose task was it ─────────────────────────────────────────────────────

def test_another_session_s_task_is_printed_and_never_picked_up(capsys):
    """Four notifications for tasks this session never started arrived with
    "pick the work back up" attached. The CLI says whose task it is —
    `session_id` is a field on the message — and this read three fields and
    threw the rest away (feedback e6c90056c2f8)."""
    stub = _repl(FakeLine(answers=["y"]))
    _note(stub, task="btrw53fkr", session="s-somewhere-else")
    assert not stub._wake.is_set(), "a foreign task must not queue a wake-up"
    printed = ANSI.sub("", capsys.readouterr().out)
    assert "btrw53fkr" in printed, "it is still worth seeing"
    assert "not this one" in printed and "not picked up here" in printed


def test_a_notification_with_no_session_is_not_claimed():
    # Cannot be shown to be ours, so it is not treated as ours. The reporter's
    # first request: route it, and if you cannot, do not inject it.
    for unknown in (None, ""):
        stub = _repl(FakeLine(answers=["y"]))
        _note(stub, session=unknown)
        assert not stub._wake.is_set(), repr(unknown)


def test_the_offer_names_the_output_file_and_the_summary():
    stub = _repl(FakeLine(answers=["y"]))
    _note(stub, summary="trained 3 epochs", output="/tmp/x/tasks/b51wc35va.output")
    line, via = _input(stub)
    assert via == "task"
    # Provenance the model can act on without guessing: what it was, and where
    # the output is.
    assert "trained 3 epochs" in line and "/tmp/x/tasks/b51wc35va.output" in line
    # And the ownership claim is checked, not decorative.
    assert "this session's own task" in line


def test_a_foreign_notification_does_not_block_our_own():
    stub = _repl(FakeLine(answers=["y"]))
    _note(stub, task="btrw53fkr", session="s-somewhere-else")
    _note(stub, task="b51wc35va")
    line, via = _input(stub)
    assert via == "task"
    assert "b51wc35va" in line and "btrw53fkr" not in line


def test_twenty_two_finished_tasks_still_fit_in_a_card_that_can_be_clicked():
    """The offer's question joined every note in full, so a session with 22
    finished tasks put thousands of characters into the control page's
    question card and it could not be approved at all (user, 2026-11-03, in
    another project). The turn text had already been bounded; the offer — the
    one a person has to read and click — had not."""
    stub = _on_page(answer=Repl._PICK_UP)
    for n in range(22):
        _note(stub, task=f"b{n:08d}", summary=f"Wire set-para-style through the api {n}" * 2,
              output=f"/tmp/claude-1001/-home-k821209-works-hwp-cmd/s/tasks/b{n:08d}.output")
    line, via = _input(stub)
    assert via == "task"
    question = stub.control.asked[0]["questions"][0]["question"]
    assert len(question) < 300, f"{len(question)} chars: {question[:120]}…"
    assert "22 background tasks finished" in question
    # The turn and the card describe the same notifications the same way.
    assert question.split(" — pick it up?")[0] in line
