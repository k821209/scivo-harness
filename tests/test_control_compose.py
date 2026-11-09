"""The dock's input can be handed a passage by the dashboard around it.

The Paper tab's "Edit with Claude" on a selection posts the passage into the
embedded control page, which COMPOSES it rather than sending: a passage on its
own is not a request, and a message that left by itself would be one nobody
wrote (user, 2026-11-09, after reading another editor's selection toolbar).

The page is pure HTML+JS with no test harness here, so these read the shipped
source for the properties that matter. That is weaker than running it, and it
is written down rather than implied: what it can catch is a listener that was
removed, renamed, or left open to any origin.
"""
from __future__ import annotations

import re

from scivo_harness import control


def _page() -> str:
    return control.page_html()


def _listener() -> str:
    """Just the message listener, cut at its own closing brace.

    A fixed-size window read past the end and into `async function send()`,
    so the "it must not send" assertion passed on the next function's name.
    """
    page = _page()
    start = page.index('addEventListener("message"')
    end = page.index("\n  });", start)
    return page[start:end]


def test_the_page_listens_for_a_composed_passage():
    page = _page()
    assert 'addEventListener("message"' in page
    assert '"scivo-control:compose"' in page


def test_it_refuses_a_message_from_another_origin():
    """The page runs in the dashboard's origin, so anything else is not the
    dashboard — and accepting it would let any page that can embed this one
    type into the session."""
    page = _page()
    body = _listener()
    assert "e.origin !== location.origin" in body
    assert "return" in body


def test_it_refuses_a_message_from_anything_but_its_embedder():
    body = _listener()
    assert "e.source !== window.parent" in body
    # And not from itself when it is NOT embedded, where window.parent is the
    # page: then there is no dashboard around it to trust.
    assert "window.parent === window" in body


def test_it_composes_instead_of_sending():
    body = _listener()
    assert "input.value" in body and "input.focus()" in body
    # The one thing that must NOT be in there.
    assert "send()" not in body, "composing must not dispatch the message"


def test_a_composed_passage_is_bounded():
    body = _listener()
    assert re.search(r"slice\(0,\s*\d+\)", body), "an unbounded paste is a denial of service"


def test_it_appends_rather_than_replacing_what_was_typed():
    """Two passages in a row, or a half-typed instruction, must not be lost."""
    body = _listener()
    assert "input.value.trim()" in body, "it has to look before overwriting"


def test_it_acknowledges_so_a_stale_page_is_not_mistaken_for_success():
    """postMessage never fails.

    A page published before this listener existed ignores the message in
    silence, and the dashboard button that sent it would still report success
    — which is exactly what happened: every one of the 24 live control pages
    predated the listener, the button said nothing, and the passage went
    nowhere (user, 2026-11-09).
    """
    body = _listener()
    assert "scivo-control:composed" in body
    assert "e.source.postMessage" in body
    # Back to where it came from, at that origin — not "*".
    assert "e.origin" in body
    assert '"*"' not in body


def test_the_acknowledgement_cannot_break_the_compose():
    """A frame that has gone away must not stop the text being put in."""
    body = _listener()
    ack = body[body.index("scivo-control:composed") - 120:]
    assert "try {" in ack and "catch" in ack
