"""How much context a local server's session gets, and saying when it is odd.

The budget was `max(8192, window - max_output_tokens)` and nothing else. A
provider that set `max_output_tokens` to its server's `-c` got
`max(8192, 0)` = 8192 — a plausible number, a common context size, and in that
report also the port the server happened to be on. The user and the session
both read it as "the server is running at 8k" and spent ten minutes looking at
the server (feedback f484ae1a9364).
"""
from __future__ import annotations

import pytest

from scivo_harness.providers import (
    CONTEXT_FLOOR, DEFAULT_REPLY_ROOM, context_budget,
)


def test_the_ordinary_case_is_the_subtraction_and_says_nothing():
    assert context_budget(32768, 4096) == (28672, None)
    assert context_budget(131072, 8192) == (122880, None)


def test_a_provider_that_names_no_reply_room_gets_the_default():
    budget, note = context_budget(16384, None)
    assert budget == 16384 - DEFAULT_REPLY_ROOM
    assert note is None


def test_reply_room_equal_to_the_window_is_reported_not_absorbed():
    """The exact reported configuration: -c 32768 and max_output_tokens 32768."""
    budget, note = context_budget(32768, 32768)
    # NOT 8192. The contradictory value is not used at all.
    assert budget == 32768 - DEFAULT_REPLY_ROOM == 28672
    assert note
    # Both numbers, so the user can see which is which.
    assert "32,768" in note
    # And what the field actually means, which is what was misread.
    assert "ONE ANSWER" in note
    assert "not the context size" in note


def test_reply_room_larger_than_the_window_is_the_same_contradiction():
    budget, note = context_budget(8192, 16384)
    assert note and "not smaller than" in note
    assert budget >= CONTEXT_FLOOR


def test_hitting_the_floor_is_said_out_loud():
    """So 8192 as a floor is distinguishable from 8192 as a result — the
    reporter's second ask, and the cheaper half of the fix."""
    budget, note = context_budget(10000, 8000)
    assert budget == CONTEXT_FLOOR
    assert note and "floor" in note
    # The arithmetic that led there, so it can be checked.
    assert "2,000" in note and "10,000" in note and "8,000" in note


def test_a_window_at_the_floor_is_not_called_clamped():
    # window - room lands exactly on the floor: nothing was clamped.
    assert context_budget(CONTEXT_FLOOR + 4096, 4096) == (CONTEXT_FLOOR, None)


def test_a_real_budget_is_never_silently_replaced_by_the_floor():
    """The property, over a sweep rather than three cases somebody picked."""
    for window in (4096, 8192, 16384, 32768, 65536, 131072):
        for room in (None, 512, 1024, 4096, 8192, 32768, 131072):
            budget, note = context_budget(window, room)
            assert budget >= CONTEXT_FLOOR
            expected = window - (room or DEFAULT_REPLY_ROOM)
            if budget != expected:
                # Every departure from the subtraction is explained.
                assert note, (window, room, budget)


def test_the_example_says_what_the_field_does():
    """The documentation half of the report: the field's second role — being
    subtracted from the window — was nowhere, and the reporter read it as
    "match the server's -c", which is what caused this."""
    from scivo_harness.providers import example_text

    text = example_text()
    assert "ONE ANSWER" in text
    assert "subtracted from" in text
    # And the shipped example no longer models the mistake.
    assert "max_output_tokens = 8192" not in text


def test_the_note_reaches_the_session(monkeypatch):
    """Computed and then dropped is the same as never computed.

    The note rides on `Endpoint.note`, which the session prints at startup —
    the one place the user is already looking. A falsification that removed it
    from the Endpoint passed every test above, because they all stop at the
    arithmetic.
    """
    from scivo_harness import providers
    from scivo_harness.providers import Provider, prepare

    monkeypatch.setattr(providers, "reachable", lambda p, timeout=5.0: None)
    monkeypatch.setattr(providers, "context_window", lambda p, timeout=5.0: 32768)
    monkeypatch.setattr(providers, "system_messages_ok",
                        lambda url, p, timeout=5.0: True)
    bad = Provider(name="gpu8192", base_url="http://localhost:8192",
                   max_output_tokens=32768)
    endpoint = prepare(bad)
    assert endpoint.note and "ONE ANSWER" in endpoint.note
    assert endpoint.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "28672"

    good = Provider(name="ok", base_url="http://localhost:8192",
                    max_output_tokens=4096)
    assert prepare(good).note is None
