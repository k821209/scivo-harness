"""A typed line is untrusted input, and `os.stat` does not answer — it raises.

`image_path_in` runs on every line that reaches the REPL, to spot someone
dropping an image path at the prompt. It asked the filesystem about the raw
line, and a filename component over NAME_MAX (255 on Linux) makes `stat` raise
rather than return False. A 700-character task notification therefore killed
the whole session from inside the dispatch loop, where nothing catches it:

    OSError: [Errno 36] File name too long: "[scivo] task bt8mmz5t5 completed …"

The notification was long because of a harness feature, but the crash was here,
and a person pasting a long sentence with a space in it would have found it too.
"""
from __future__ import annotations

from scivo_harness.attachments import image_path_in


def test_a_long_sentence_is_not_a_path_and_does_not_raise():
    prose = "[scivo] task bt8mmz5t5 completed (Locate the harness source on disk); " * 9
    assert len(prose) > 255
    assert image_path_in(prose) is None


def test_one_over_long_component_is_enough():
    assert image_path_in("/tmp/" + "x" * 300 + ".png") is None


def test_a_long_line_of_short_components_is_still_not_a_crash():
    # Under NAME_MAX per component but far over it in total: `stat` answers
    # here (ENAMETOOLONG for the whole path on some systems), so the guard is
    # the try/except rather than the length check.
    assert image_path_in("/".join(["dir"] * 400) + "/x.png") is None


def test_rubbish_bytes_are_not_a_path():
    for bad in ("a\x00b.png", "\udcff.png"):
        assert image_path_in(bad) is None, bad


def test_a_real_image_path_still_resolves(tmp_path):
    png = tmp_path / "figure 1.png"      # a space, so the exists() branch runs
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert image_path_in(str(png)) == png.resolve()
    assert image_path_in(f"'{png}'") == png.resolve()


def test_a_path_that_is_not_an_image_is_ignored(tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("hi")
    assert image_path_in(str(other)) is None


def test_a_path_too_long_for_the_filesystem_but_short_in_every_part():
    # The case the NAME_MAX fast path does NOT cover: every component is tiny,
    # the whole path is over PATH_MAX, and it has a space so the existence
    # branch runs. `os.stat` raises ENAMETOOLONG here, so the try/except is
    # what keeps this a None instead of a dead session.
    assert image_path_in("a " + "/".join(["dir"] * 2000)) is None
