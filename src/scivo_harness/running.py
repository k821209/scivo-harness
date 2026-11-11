"""Which code THIS PROCESS is running — asked once, when it starts.

"업뎃은 다 한 거 같은데" was said three times in two days, and each time the
answer had to be worked out from uptimes: `scivo update` replaces the files,
and a session that was already running keeps the code it loaded. Twenty
sessions were up eighteen hours on yesterday's build with nothing on any
screen to say so, and the cost of the build they were on was landing on the
service the whole time.

So a session says what it is running, in the page head it already writes,
where the operator's dashboard can list it. Read at import and never again:
the question is what this process LOADED, and the disk can change under it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _detect() -> str:
    here = Path(__file__).resolve().parent
    # A clone or an editable install: the commit of the tree we were loaded from.
    for root in (here.parent.parent, here.parent):
        if (root / ".git").exists():
            try:
                out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=7", "HEAD"],
                                     capture_output=True, text=True, timeout=5)
                if out.returncode == 0 and out.stdout.strip():
                    return out.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
    # A snapshot installed from git: the commit pip recorded for it.
    try:
        import importlib.metadata as md
        import json
        raw = md.distribution("scivo-harness").read_text("direct_url.json")
        commit = (json.loads(raw).get("vcs_info") or {}).get("commit_id") if raw else None
        if commit:
            return str(commit)[:7]
    except Exception:  # noqa: BLE001 — not knowing is an answer here, below
        pass
    return ""


# "" means it could not be determined — which the reader shows as unknown,
# never as current.
HARNESS = _detect()
