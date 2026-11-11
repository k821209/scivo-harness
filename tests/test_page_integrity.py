"""Every file the control page loads from elsewhere is pinned by its bytes.

The page runs in the dashboard's origin and drives a shell. It loaded three
scripts and a stylesheet from a CDN on the strength of a version number
(review 2026-11-10); with `integrity` the browser refuses a file whose bytes
changed. This test cannot check the hashes are RIGHT without the network —
tests/browser/*.spec.mjs load the page in a real Chrome, which is where a
wrong hash shows as a page that does not start — but it does stop a new
remote file being added with no hash at all.
"""
from __future__ import annotations

import re

from scivo_harness.control import page_html

TAG = re.compile(r"<(script|link)\b[^>]*>", re.I | re.S)
REMOTE = re.compile(r"""\b(?:src|href)\s*=\s*["'](https?:)?//""", re.I)


def _remote_tags() -> list[str]:
    return [m.group(0) for m in TAG.finditer(page_html()) if REMOTE.search(m.group(0))]


def test_the_page_does_load_remote_files_so_this_test_is_about_something():
    assert len(_remote_tags()) >= 4


def test_every_remote_file_carries_a_sha384_and_is_fetched_without_credentials():
    for tag in _remote_tags():
        assert re.search(r'integrity="sha384-[A-Za-z0-9+/]{64}"', tag), tag
        assert 'crossorigin="anonymous"' in tag, tag


def test_every_remote_file_names_an_exact_version():
    """A hash on `marked@12` would break the page the day 12.0.3 ships; a
    hash on a moving target is a time bomb rather than a pin."""
    for tag in _remote_tags():
        assert re.search(r"@\d+\.\d+\.\d+/", tag), tag
