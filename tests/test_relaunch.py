"""`/update` has to hand the session back in the state it was in.

Leaving, updating and coming back was four commands typed by hand, so /update
does it — and it already restored the control page, because the page is part
of "this session". The permission mode and the model are too, and neither
lives in argv: `/dangerously-skip-permissions` and `/model` switch them
mid-session, so an update handed the session back in whatever mode it had
STARTED in. Half way through an unattended run, that means it quietly starts
asking again (user, 2026-10-19).
"""
from __future__ import annotations

from scivo_harness.cli import _relaunch_argv


def test_a_mode_switched_mid_session_comes_back():
    argv = _relaunch_argv(["-c"], "sess-1", False,
                          permission_mode="bypassPermissions", model="claude-opus-5")
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--resume") + 1] == "sess-1"


def test_the_live_mode_replaces_the_one_argv_started_with():
    """Started in default, switched to bypass: the switch is what comes back."""
    for started in (["--permission-mode", "default"], ["--permission-mode=default"]):
        argv = _relaunch_argv([*started, "--profile", "video"], "s", False,
                              permission_mode="bypassPermissions")
        assert argv.count("--permission-mode") == 1, argv
        assert "default" not in argv, argv
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
        # An unrelated flag the run was started with survives.
        assert argv[argv.index("--profile") + 1] == "video"


def test_a_mode_switched_back_off_stays_off():
    argv = _relaunch_argv(["--permission-mode", "bypassPermissions"], "s", False,
                          permission_mode="default")
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv.count("bypassPermissions") == 0


def test_the_model_travels_but_only_when_it_was_given():
    with_model = _relaunch_argv(["--model", "claude-sonnet-5"], "s", False,
                                permission_mode="default", model="claude-opus-5")
    assert with_model[with_model.index("--model") + 1] == "claude-opus-5"
    assert "claude-sonnet-5" not in with_model
    # A local provider switch starts a new conversation, so no model is carried.
    local = _relaunch_argv(["--provider", "local"], "s", False,
                           permission_mode="default", model=None)
    assert "--model" not in local
    assert local[local.index("--provider") + 1] == "local"


def test_the_control_page_and_session_choice_still_behave():
    argv = _relaunch_argv(["--control", "-c", "chat"], None, True)
    assert argv.count("--control") == 1
    assert "-c" in argv and "chat" not in argv
    assert "--resume" not in argv


def test_nothing_is_added_when_there_is_nothing_to_carry():
    assert _relaunch_argv(["--profile", "deck"], "s", False) == [
        "--profile", "deck", "--resume", "s"]


# ── the provider travels by name (user, 2026-11-14) ─────────────────────────
# "/update 했을 때 로컬모델을 쓴 경우에 그대로 상속이 안 되고 405 에러가 뜬다.
# 다시 /model 해서 지정하면 문제는 없다." The relaunch carried no provider,
# so the resume worked it out from the model's name — and `local` is the name
# of every local server.

def test_a_provider_switched_to_mid_session_comes_back():
    argv = _relaunch_argv(["--profile", "video"], "sess-9", True,
                          permission_mode="default", model="local", provider="gpu8192")
    assert argv[argv.index("--provider") + 1] == "gpu8192"
    assert argv[argv.index("--model") + 1] == "local"
    assert argv[argv.index("--resume") + 1] == "sess-9"


def test_the_live_provider_replaces_the_one_the_run_started_with():
    back_on_claude = _relaunch_argv(["--provider", "local"], "s", False,
                                    model="claude-opus-5", provider="anthropic")
    assert back_on_claude.count("--provider") == 1
    assert back_on_claude[back_on_claude.index("--provider") + 1] == "anthropic"
    assert "local" not in back_on_claude
    equals_form = _relaunch_argv(["--provider=local"], "s", False, provider="gpu8192")
    assert "--provider=local" not in equals_form and "gpu8192" in equals_form


def test_a_provider_not_yet_spoken_to_comes_back_as_a_new_conversation():
    """No session id: `-c` would continue the LAST conversation — another
    model's — on this provider."""
    argv = _relaunch_argv(["-c", "--profile", "video"], None, False,
                          model="local", provider="gpu8192", fresh=True)
    assert "-c" not in argv and "--resume" not in argv
    assert argv[argv.index("--provider") + 1] == "gpu8192"
    # and without `fresh`, an update with nothing to resume still continues
    assert "-c" in _relaunch_argv(["--profile", "video"], None, False, provider="anthropic")


def test_update_asks_for_the_provider_the_session_is_on(monkeypatch):
    import types
    from scivo_harness.repl import Repl

    def plan(provider_name, has_endpoint, session_id):
        provider = types.SimpleNamespace(name=provider_name, has_endpoint=has_endpoint)
        stub = types.SimpleNamespace(
            session=types.SimpleNamespace(provider=provider, model="local" if has_endpoint else "claude-opus-5"),
            session_id=session_id, control=None, mode="default", relaunch=None)
        try:
            Repl._local(stub, "/update")
        except EOFError:
            pass
        return stub.relaunch

    local = plan("gpu8192", True, "sess-1")
    assert (local["provider"], local["model"], local["fresh"]) == ("gpu8192", "local", False)
    unspoken = plan("gpu8192", True, None)
    assert unspoken["fresh"] is True
    claude = plan("anthropic", False, None)
    assert claude["provider"] == "anthropic" and claude["fresh"] is False


# ── and a plain resume knows which provider a conversation was on ────────────

def _two_locals(monkeypatch):
    import types
    from scivo_harness import providers
    table = {"anthropic": types.SimpleNamespace(model=None, small_model=None),
             "local": types.SimpleNamespace(model="local", small_model=None),
             "gpu8192": types.SimpleNamespace(model="local", small_model=None)}
    monkeypatch.setattr(providers, "load_all", lambda: table)


def _args():
    import types
    return types.SimpleNamespace(provider="anthropic", provider_reason="default", model=None)


def test_a_resume_takes_the_noted_provider_not_the_first_with_that_model(tmp_path, monkeypatch):
    from scivo_harness import cli, sessions
    _two_locals(monkeypatch)
    monkeypatch.setattr(sessions, "last_model", lambda sid: "local")
    sessions.remember(tmp_path, "sess-1", "gpu8192", "local")
    args = _args()
    cli._carry_model(args, "sess-1", tmp_path)
    assert (args.provider, args.model) == ("gpu8192", "local")


def test_without_a_note_two_providers_with_one_model_name_are_said(tmp_path, monkeypatch, capsys):
    from scivo_harness import cli, sessions
    _two_locals(monkeypatch)
    monkeypatch.setattr(sessions, "last_model", lambda sid: "local")
    args = _args()
    cli._carry_model(args, "old-session", tmp_path)
    said = capsys.readouterr().out
    assert args.provider == "local"                       # the first, as before
    assert "2 providers here have one (local, gpu8192)" in said and "/model <provider>" in said


def test_a_noted_provider_that_no_longer_exists_falls_back_to_the_name(tmp_path, monkeypatch):
    from scivo_harness import cli, sessions
    _two_locals(monkeypatch)
    monkeypatch.setattr(sessions, "last_model", lambda sid: "local")
    sessions.remember(tmp_path, "sess-1", "removed-box", "local")
    args = _args()
    cli._carry_model(args, "sess-1", tmp_path)
    assert args.provider == "local"


def test_an_explicit_provider_is_not_overridden(tmp_path, monkeypatch):
    from scivo_harness import cli, sessions
    _two_locals(monkeypatch)
    sessions.remember(tmp_path, "sess-1", "gpu8192", "local")
    args = _args()
    args.provider, args.provider_reason = "local", "--provider"
    cli._carry_model(args, "sess-1", tmp_path)
    assert args.provider == "local"


def test_the_note_is_small_bounded_and_never_fatal(tmp_path):
    from scivo_harness import sessions
    for i in range(sessions._KEPT + 20):
        sessions.remember(tmp_path, f"s{i}", "gpu8192", "local")
    import json
    known = json.loads((tmp_path / sessions.SESSIONS_FILE).read_text())
    assert len(known) == sessions._KEPT and "s0" not in known and f"s{sessions._KEPT + 19}" in known
    assert sessions.remembered(tmp_path, "nope") is None
    sessions.remember(tmp_path / "no" / "such" / "\0bad", "s", "p", "m")      # must not raise
    (tmp_path / sessions.SESSIONS_FILE).write_text("not json")
    assert sessions.remembered(tmp_path, "s1") is None
    sessions.remember(tmp_path, "s1", "gpu8192", "local")                       # recovers
    assert sessions.remembered(tmp_path, "s1")["provider"] == "gpu8192"


# ── putting the provider right must not cost the conversation ───────────────
# msja, 2026-11-14: back from a restart on the wrong provider (405), the user
# typed `/model gpu8192` — which started a NEW conversation, and "멈췄던 작업
# 계속 진행" then went to a model that had nothing to continue.

def _switch(monkeypatch, tmp_path, session_id, held_with):
    import asyncio
    import types
    from scivo_harness import guardrails, providers, sessions
    from scivo_harness.providers import Provider
    from scivo_harness.repl import Repl

    wrong = Provider(name="local", base_url="http://localhost:8080", model="local")
    right = Provider(name="gpu8192", base_url="http://localhost:8192", model="local")
    built: dict = {}

    def options_for(target, **changes):
        built.update(changes)
        return types.SimpleNamespace(**changes)

    session = types.SimpleNamespace(
        model="local", provider=wrong, options=types.SimpleNamespace(effort=None),
        options_for=options_for, endpoint=None, lean_tools=None,
        rails=guardrails.Guardrails(), config=types.SimpleNamespace(root=tmp_path),
        kit=lambda: types.SimpleNamespace(kept=[], lean=True, summary=lambda: "kit"))
    said: list[str] = []
    stub = types.SimpleNamespace(session=session, session_id=session_id, control=None, said=said)
    stub._say = said.append
    stub._model_rows = Repl._model_rows.__get__(stub)
    stub._note_provider = Repl._note_provider.__get__(stub)

    async def swap(options):
        stub.swapped = options
    stub._swap_client = swap
    monkeypatch.setattr(providers, "load_all", lambda: {
        "anthropic": Provider(name="anthropic"), "local": wrong, "gpu8192": right})
    monkeypatch.setattr(providers, "prepare",
                        lambda target: types.SimpleNamespace(env={}, note="", close=lambda: None))
    monkeypatch.setattr(sessions, "last_model", lambda sid: held_with)
    asyncio.run(Repl._model(stub, "gpu8192"))
    return stub, built, "\n".join(said)


def test_a_switch_to_the_model_the_conversation_was_held_with_keeps_it(monkeypatch, tmp_path):
    from scivo_harness import sessions
    stub, built, said = _switch(monkeypatch, tmp_path, "sess-local", held_with="local")
    assert built["resume"] == "sess-local"
    assert stub.session_id == "sess-local"
    assert "so it continues" in said and "new conversation" not in said
    assert sessions.remembered(tmp_path, "sess-local")["provider"] == "gpu8192"
    assert stub.session.rails.turn_budget == 40 and "stops after 40 tool calls" in said


def test_a_conversation_held_with_claude_still_starts_again(monkeypatch, tmp_path):
    stub, built, said = _switch(monkeypatch, tmp_path, "sess-claude", held_with="claude-opus-5-5")
    assert built["resume"] is None and stub.session_id is None
    assert "this starts a new conversation" in said


def test_with_no_conversation_yet_there_is_nothing_to_keep(monkeypatch, tmp_path):
    stub, built, _said = _switch(monkeypatch, tmp_path, None, held_with="local")
    assert built["resume"] is None and stub.session_id is None


def test_the_projects_own_provider_breaks_a_tie_and_the_guess_is_kept_for_the_page(
        tmp_path, monkeypatch, capsys):
    from scivo_harness import cli, sessions
    _two_locals(monkeypatch)
    monkeypatch.setattr(sessions, "last_model", lambda sid: "local")
    args = _args()
    args.provider, args.provider_reason = "gpu8192", "set with `scivo providers use`"
    cli._carry_model(args, "old-session", tmp_path)
    assert args.provider == "gpu8192"                      # not `local`, the first listed
    assert "continuing on gpu8192" in args.resume_note
    assert "the conversation is kept" in args.resume_note

