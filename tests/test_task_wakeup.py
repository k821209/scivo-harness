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
        notify_mode=notify,
        _wake=asyncio.Event(),
        _wake_notes=[],
        said=[],
    )
    stub._say = stub.said.append
    for name in ("_next_input", "_on_task_note", "_consume_wake", "_wake_line",
                 "_announce_wake", "_offer_wake", "_notify"):
        setattr(stub, name, getattr(Repl, name).__get__(stub))
    return stub


def _note(stub, task="b51wc35va", status="completed", summary=""):
    stub._on_task_note(types.SimpleNamespace(task_id=task, status=status, summary=summary))


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


def test_five_notifications_are_one_offer_naming_all_of_them():
    stub = _repl(FakeLine(answers=["y"]))
    for n in range(5):
        _note(stub, task=f"task{n}")
    line, via = _input(stub)
    assert via == "task" and stub.line.asked == 1
    assert all(f"task{n}" in line for n in range(5))
    assert not stub._wake.is_set() and stub._wake_notes == []


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
