"""What this process sends over the network, in the same file as the MCP's.

The MCP writes its SSH calls to `~/.co-scientist/net.log` so a dropped link can
be lined up against everything that was in flight (todo d27e0606a7bb). The
harness's own traffic — the control page's channel above all — was not in it:
the busiest periodic thing a session did, twice a second, was the one thing
the log could not show (review 2026-11-10).

A line per call would bury the file, so ordinary calls are TALLIED and written
as one line per kind per minute. A call that failed or was slow is written at
once, on its own line: those are the ones a dropped link produces, and the
reason anyone opens this file.
"""

from __future__ import annotations

import time

SLOW_MS = 2000            # a call this slow is worth its own line
TALLY_EVERY = 60.0        # seconds between summary lines


def record(*args, **kwargs) -> None:
    """One line in the MCP's outbound-call log, if this install has one. An
    older MCP has no netlog, and that is not worth an error."""
    try:
        from co_scientist_local import netlog
    except Exception:  # noqa: BLE001 — no log is not a failure
        return
    try:
        netlog.record(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return


class Tally:
    """Counts ordinary calls by kind; writes anomalies as they happen."""

    def __init__(self, channel: str = "mcp", host: str = "firestore",
                 every: float = TALLY_EVERY, sink=None) -> None:
        self.channel, self.host, self.every = channel, host, every
        self._sink = sink or record
        self._counts: dict[str, list[int]] = {}       # kind -> [n, total ms, max ms]
        self._since = time.monotonic()

    def add(self, kind: str, ms: float, ok: bool, note: str = "") -> None:
        ms = int(ms)
        if not ok or ms >= SLOW_MS:
            self._sink(self.channel, host=self.host, kind=kind, ms=ms,
                       rc=0 if ok else 1, note=note or ("slow" if ok else "failed"))
        else:
            entry = self._counts.setdefault(kind, [0, 0, 0])
            entry[0] += 1
            entry[1] += ms
            entry[2] = max(entry[2], ms)
        if time.monotonic() - self._since >= self.every:
            self.flush()

    def flush(self) -> None:
        """Write what has been counted. Called on the clock and on the way out
        — a session that stops must not take its last minute with it."""
        counts, self._counts = self._counts, {}
        self._since = time.monotonic()
        for kind, (n, total, worst) in sorted(counts.items()):
            self._sink(self.channel, host=self.host, kind=kind, ms=total // max(n, 1),
                       rc=0, note=f"n={n} max={worst}ms")
