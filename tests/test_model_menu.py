"""What `/model` shows, and what a number picks.

`/model` used to print its four aliases in a dim hint and nothing else. Those
aliases pointed one generation back, so Opus 5.5 read as unavailable even
though an exact id has always been accepted — the choices existed and could
not be seen (user, 2026-11-02: "가능한 모든 모델을 선택할 수 있게 하고
선택지를 보여주자"). `/model` with no argument is now the menu, and the number
on each line picks it.

The menu and the number come from one list on purpose: printing from one and
resolving from another is how a person picks line 3 and gets line 4.
"""
from __future__ import annotations

import asyncio
import types

from scivo_harness.providers import Provider
from scivo_harness.repl import ANSI, Repl
from scivo_harness.session import CLAUDE_CATALOG


def _stub(model="claude-opus-5-5", provider=None):
    """Just enough of a Repl for `/model` — it touches session, client, control."""
    said: list[str] = []
    switched: list[str] = []

    async def set_model(name):
        switched.append(name)

    stub = types.SimpleNamespace(
        session=types.SimpleNamespace(model=model,
                                      provider=provider or Provider(name="anthropic")),
        client=types.SimpleNamespace(set_model=set_model),
        control=None,
        said=said,
        switched=switched,
    )
    stub._say = said.append
    stub._model_rows = Repl._model_rows.__get__(stub)
    return stub


def _run(stub, argument, providers=None):
    import scivo_harness.providers as providers_module

    original = providers_module.load_all
    providers_module.load_all = lambda: providers or {"anthropic": Provider(name="anthropic")}
    try:
        asyncio.run(Repl._model(stub, argument))
    finally:
        providers_module.load_all = original
    return ANSI.sub("", "\n".join(stub.said))


def test_the_menu_numbers_every_model_in_the_catalog():
    screen = _run(_stub(), "")
    for index, (alias, (model, _)) in enumerate(CLAUDE_CATALOG.items(), start=1):
        assert f"{index:>2}. {alias}" in screen, f"line {index} is not {alias}"
        assert model in screen
    # Every id, not only the four current ones: the previous generation is a
    # real choice and was invisible before.
    assert "claude-opus-5-5" in screen and "claude-opus-5 " in screen


def test_the_menu_marks_the_model_in_use():
    screen = _run(_stub(model="claude-haiku-5-5"), "")
    marked = [line for line in screen.splitlines() if "← this session" in line]
    assert len(marked) == 1 and "claude-haiku-5-5" in marked[0]


def test_a_configured_provider_is_listed_under_its_own_name():
    local = Provider(name="qwen-test", base_url="http://x/v1", model="qwen3-30b")
    screen = _run(_stub(), "", providers={"anthropic": Provider(name="anthropic"),
                                          "qwen-test": local})
    line = [l for l in screen.splitlines() if "qwen-test" in l][-1]
    # The switch below takes a provider NAME, so that is what the menu offers.
    assert line.index("qwen-test") < line.index("qwen3-30b")


def test_the_number_on_a_line_picks_that_model():
    aliases = list(CLAUDE_CATALOG)
    for number, alias in ((1, aliases[0]), (3, aliases[2]), (len(aliases), aliases[-1])):
        stub = _stub()
        _run(stub, str(number))
        assert stub.switched == [CLAUDE_CATALOG[alias][0]], f"/model {number} picked wrong"
        assert stub.session.model == CLAUDE_CATALOG[alias][0]


def test_a_number_past_the_end_is_refused_without_switching():
    stub = _stub()
    screen = _run(stub, str(len(CLAUDE_CATALOG) + 99))
    assert stub.switched == []
    assert "no choice" in screen


def test_an_alias_and_a_bare_id_both_still_work():
    for typed, expected in (("sonnet", "claude-sonnet-5-5"),
                            ("claude-opus-5", "claude-opus-5"),
                            ("claude-nonesuch-9", "claude-nonesuch-9")):
        stub = _stub()
        _run(stub, typed)
        # A model the catalog has never heard of is passed through, so one
        # released tomorrow is usable the day it ships.
        assert stub.switched == [expected], typed


def test_the_prompt_completes_model_names_after_the_command():
    from prompt_toolkit.document import Document

    from scivo_harness.prompt_line import _build_completer

    completer = _build_completer({"/model": "switch model"}, [])
    offered = [c.text for c in completer.get_completions(Document("/model op"), None)]
    assert "opus" in offered and "opus-5" in offered
    assert "sonnet" not in offered
    # An exact id completes too, from the same table.
    ids = [c.text for c in completer.get_completions(Document("/model claude-h"), None)]
    assert "claude-haiku-5-5" in ids


def test_fable_names_the_newest_fable_and_the_one_it_replaced_stays_reachable():
    # Added 2026-11-10 with claude-fable-5-2. An alias moving forward must not
    # make the old id reachable only by someone who remembers it.
    stub = _stub()
    _run(stub, "fable")
    assert stub.switched == ["claude-fable-5-2"]
    stub = _stub()
    _run(stub, "fable-5-1")
    assert stub.switched == ["claude-fable-5-1"]
