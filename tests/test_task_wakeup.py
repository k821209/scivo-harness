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
import json
import tempfile
import types
from pathlib import Path

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


def _repl(line: FakeLine | None = None, notify: str = "ask", root=None,
          session_id: str = "a2d62845-99d9-4bca-9096-e53dbcec4d76"):
    # A real project root, because ownership is now also written to
    # `<root>/.scivo/tasks.json` — the set alone does not survive a restart.
    root = Path(root or tempfile.mkdtemp())
    stub = types.SimpleNamespace(
        session=types.SimpleNamespace(config=types.SimpleNamespace(root=root)),
        line=line or FakeLine(),
        control=None,
        mode="default",
        _typed_ahead="",
        session_id=session_id,
        notify_mode=notify,
        _wake=asyncio.Event(),
        _wake_notes=[],
        said=[],
    )
    stub._say = stub.said.append
    for name in ("_next_input", "_on_task_note", "_on_task_update", "_consume_wake",
                 "_wake_line", "_announce_wake", "_offer_wake", "_offer_on_page",
                 "_notify", "_tasks_file", "_read_task_store", "_stored_task_ids",
                 "_write_task_store", "_remember_task", "_forget_task",
                 "_task_ownership", "_session_in_output"):
        setattr(stub, name, getattr(Repl, name).__get__(stub))
    stub._own_tasks = set()
    # The card's labels are read back off the answer, so the stub takes them
    # from the class rather than repeating the strings.
    stub._PICK_UP, stub._NOT_NOW = Repl._PICK_UP, Repl._NOT_NOW
    return stub


def _start(stub, task, session=...):
    """The session watching a task begin — which is what makes it ours.

    Not `session_id` on the completion notice: one naming a task that exists
    nowhere on this machine arrived carrying OUR session id, so that field
    does not answer "whose task is this" (2026-11-04).
    """
    stub._on_task_update(types.SimpleNamespace(
        task_id=task, status="running", patch={},
        session_id=stub.session_id if session is ... else session))


def _note_output(task, session_dir="a2d62845-99d9-4bca-9096-e53dbcec4d76"):
    """The path the CLI writes a background task's output to."""
    return f"/tmp/claude-1001/-home-k821209-works/{session_dir}/tasks/{task}.output"


def _note(stub, task="b51wc35va", status="completed", summary="", session=...,
          output=..., watched=True, session_dir=""):
    """A notification as the CLI delivers it.

    `output` defaults to the file a REAL background task writes; pass "" for a
    foreground call's completion, which carries none. `watched=False` posts one
    for a task this session never saw start.
    """
    if output is ...:
        # The real layout: the owning session's id is a segment of the path.
        output = _note_output(task, session_dir
                              or "a2d62845-99d9-4bca-9096-e53dbcec4d76")
    if watched:
        _start(stub, task, session)
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

def test_a_task_this_session_never_saw_start_is_printed_and_never_picked_up(capsys):
    """Notifications for tasks this session never started arrived with "pick
    the work back up" attached (feedback e6c90056c2f8). Filtering on the
    notice's `session_id` looked like the answer and was not: one naming a task
    that exists nowhere on this machine came carrying OUR session id, and was
    announced as "this session's own task" (2026-11-04). Ownership is what we
    watched begin — and when nothing settles it, it is left UNSETTLED rather
    than guessed either way."""
    stub = _repl(FakeLine(answers=["y"]))
    # Our own id on the notice — the field that lied once — and NO other
    # evidence: no sighting, no record, and an output path that names no
    # session. Nothing can show it is ours, so it is not treated as ours.
    _note(stub, task="btrw53fkr", watched=False,
          output="/tmp/claude-1001/-home-k821209-works/no-uuid-here/tasks/btrw53fkr.output")
    assert not stub._wake.is_set(), "nothing shows it is ours — do not act on it"
    printed = ANSI.sub("", capsys.readouterr().out)
    assert "btrw53fkr" in printed, "it is still worth seeing"
    assert "cannot tell whose task" in printed
    # NOT claimed as somebody else's: that is the reading that misled, since
    # the usual cause is this session's own task from before a restart.
    assert "another session" not in printed


def test_the_output_path_settles_a_task_from_before_a_restart(capsys):
    """The case the user hit right after upgrading (2026-11-06).

    A task was already running when `.scivo/tasks.json` did not yet exist, so
    there was no record and this process had not watched it start — and it came
    back "could not be determined". But the CLI had been writing its output
    under this session's own folder the whole time:
    `/tmp/claude-<uid>/<cwd>/<session-id>/tasks/<task>.output`. That is where
    the file actually landed, which is better evidence than the notice's
    `session_id` field, and it resolves every in-flight task retroactively.
    """
    stub = _repl(FakeLine(answers=["y"]))
    _note(stub, task="bhul0ysn3", watched=False,
          summary="Measure error rate by base quality using pbsim MAF truth")
    assert stub._own_tasks == set(), "never seen start, no record — only the path"
    assert stub._task_ownership("bhul0ysn3", "", _note_output("bhul0ysn3")) == "own"
    printed = ANSI.sub("", capsys.readouterr().out)
    assert "cannot tell" not in printed and "another session" not in printed


def test_an_output_path_under_another_session_is_foreign(capsys):
    stub = _repl(FakeLine(answers=["y"]))
    _note(stub, task="bq9zz1abc", watched=False, session_dir="11111111-2222-3333-4444-555555555555")
    assert not stub._wake.is_set()
    printed = ANSI.sub("", capsys.readouterr().out)
    assert "another session started task" in printed


def test_a_notice_from_a_different_session_says_so(capsys):
    stub = _repl(FakeLine(answers=["y"]))
    _note(stub, task="bq9zz1abc", watched=False, session="s-somebody-else",
          output="/tmp/claude-1001/-home-k821209-works/plain/tasks/bq9zz1abc.output")
    assert not stub._wake.is_set()
    printed = ANSI.sub("", capsys.readouterr().out)
    assert "another session started task" in printed
    assert "not picked up here" in printed


def test_a_resumed_session_still_owns_the_task_it_started(capsys, tmp_path):
    """The reported bug (user, 2026-11-06).

    `_own_tasks` was memory only, so after `--resume` a session's own long
    task — still running across the restart — reported as another session's
    when it finished. The user could see the work was theirs ("Llm2assem 잡
    맞는것 같기도 해서"), which is the worst case: the label was confidently
    wrong about the one thing it exists to say.
    """
    before = _repl(FakeLine(), root=tmp_path, session_id="s-long-lived")
    _start(before, "bcwskkm1m")
    assert (tmp_path / ".scivo" / "tasks.json").is_file(), "it must outlive the process"

    # A NEW process, same session id — which is what a resume is.
    after = _repl(FakeLine(answers=["y"]), root=tmp_path, session_id="s-long-lived")
    # Asserted through the decision function, not through a cached copy of its
    # answer: `_own_tasks` is this process's own sightings and is empty here by
    # design — the record on disk is what carries ownership across the restart.
    assert after._own_tasks == set()
    assert after._task_ownership("bcwskkm1m", "") == "own"
    _note(after, task="bcwskkm1m", status="stopped", watched=False)
    printed = ANSI.sub("", capsys.readouterr().out)
    assert "another session" not in printed and "cannot tell" not in printed


def test_another_session_in_the_same_project_does_not_inherit_it(tmp_path):
    """The store is keyed by session id, so sharing a project is not owning."""
    mine = _repl(FakeLine(), root=tmp_path, session_id="s-one")
    _start(mine, "btask111")
    theirs = _repl(FakeLine(), root=tmp_path, session_id="s-two")
    assert "btask111" not in theirs._own_tasks
    assert theirs._task_ownership("btask111", "") == "foreign"


def test_a_finished_task_is_dropped_from_the_record(tmp_path):
    """Bookkeeping that only grows is a leak; a closed task needs no row."""
    stub = _repl(FakeLine(answers=["y"]), root=tmp_path, session_id="s-one")
    _start(stub, "btask222")
    assert "btask222" in json.loads((tmp_path / ".scivo" / "tasks.json").read_text())
    _note(stub, task="btask222", status="completed", watched=False)
    assert "btask222" not in json.loads((tmp_path / ".scivo" / "tasks.json").read_text())


def test_an_unreadable_record_is_not_fatal(tmp_path):
    (tmp_path / ".scivo").mkdir()
    (tmp_path / ".scivo" / "tasks.json").write_text("{not json")
    stub = _repl(FakeLine(), root=tmp_path, session_id="s-one")
    assert stub._own_tasks == set()
    assert stub._task_ownership("bwhatever", "") == "unknown"


def test_watching_a_task_start_is_what_makes_it_ours(capsys):
    stub = _repl(FakeLine(answers=["y"]))
    _start(stub, "b51wc35va")
    assert "b51wc35va" in stub._own_tasks
    _note(stub, task="b51wc35va", watched=False)      # already watched above
    assert stub._wake.is_set()


def test_a_notification_with_nothing_to_go_on_is_not_claimed():
    # Cannot be shown to be ours, so it is not treated as ours. The reporter's
    # first request: route it, and if you cannot, do not inject it. Every
    # source of evidence is absent here — no sighting, no record, no session on
    # the notice, and an output path that names none either.
    for unknown in (None, ""):
        stub = _repl(FakeLine(answers=["y"]))
        _note(stub, session=unknown, watched=False,
              output="/tmp/claude-1001/-home-k821209-works/plain/tasks/b51wc35va.output")
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
    _note(stub, task="btrw53fkr", watched=False, session_dir="11111111-2222-3333-4444-555555555555")
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


def test_a_foreground_call_s_completion_is_not_work_to_pick_up(capsys):
    """Five notices arrived naming this session's own Bash descriptions
    verbatim — "Make the fixtures watch tasks start" — for calls whose output
    this conversation had already used. Offering to "carry on with it" is a
    loop: the result was handed back inside the turn (2026-11-05)."""
    stub = _repl(FakeLine(answers=["y"]))
    _note(stub, task="bezpphd7e", summary="Base ownership on what the session "
                                          "watched start", output="")
    assert not stub._wake.is_set()
    printed = ANSI.sub("", capsys.readouterr().out)
    assert "ran in the foreground" in printed
    assert "nothing to pick up" in printed


def test_a_real_background_task_still_wakes_us(capsys):
    stub = _repl(FakeLine(answers=["y"]))
    _note(stub, task="bjfz4ehhy")           # the default output file
    assert stub._wake.is_set()
