"""`/scivo-control` — drive this local session from a scivo web page.

The channel is `publish_page`, repurposed. A published page runs in the
dashboard's origin with a scoped `window.scivo` client that can read the
publication's `content` and write `responses`; the owner side reads `responses`
and writes `content` over MCP. That is a two-way Firestore channel with no new
server, so the page can exist before the dashboard grows a panel for it.

What the probes established, and how the layout follows from each:

- The page subscribes (`subscribeDoc`, measured at 54–94 ms from this side's
  write to the page's callback) to `content/head` and to the transcript chunks
  that can still change. This side cannot subscribe — it reaches Firestore only
  through MCP calls, which the server answers one at a time in ~60 ms — so it
  polls the response docs, fast while the session is busy and slower when idle.
- `list` on the page cannot filter by field, and `list_responses` on this side
  returns every response ever written without document ids. So neither side
  lists anything that grows: the transcript lives in fixed-size chunk docs
  addressed by id, and the page writes to three fixed response docs.
- Page `put` merges. A map field therefore accumulates keys across writes — the
  approvals doc relies on that — while an array is replaced whole, which is what
  the inbox relies on.

Scope is one person driving their own session. The publication gets a single
passcode labelled `owner`, and every response is checked for that label before
it is acted on. A second label is a second person, and routing someone else's
prompts through this machine's Claude login is not something to do quietly.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .config import ScivoConfig
from .scivo_mcp import ScivoClient, connect

OWNER = "owner"
CHUNK = 20                 # events per log doc; keeps a doc well under Firestore's 1 MB
MAX_TEXT = 150_000         # one event's text, same reason
FLUSH_EVERY = 0.1          # seconds; streaming deltas coalesce into one write
POLL_ACTIVE = 0.15         # seconds between reads of web input while the session is busy
POLL_IDLE = 0.5            # … and once it has been quiet for ACTIVE_WINDOW
ACTIVE_WINDOW = 60.0       # seconds of quiet before polling slows down
BEAT_EVERY = 5.0           # seconds; the page shows "disconnected" past a few misses
STATE_FILE = Path(".scivo") / "control.json"


def page_html() -> str:
    return resources.files("scivo_harness").joinpath("control_page.html").read_text(encoding="utf-8")


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Link:
    pub_id: str
    url: str
    passcode: str

    def to_json(self) -> dict[str, str]:
        return {"pub_id": self.pub_id, "url": self.url, "passcode": self.passcode}


@dataclass
class _Approval:
    rid: str
    index: int
    future: asyncio.Future


@dataclass
class Control:
    """The live channel. Created by `/scivo-control`, closed by `off` or exit."""

    config: ScivoConfig
    project_name: str
    project_id: str
    model: str

    link: Link | None = None
    sid: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    messages: asyncio.Queue = field(default_factory=asyncio.Queue)
    interrupt_requested: asyncio.Event = field(default_factory=asyncio.Event)
    errors: list[str] = field(default_factory=list)

    _client: ScivoClient | None = None
    _stack: AsyncExitStack | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _dirty: set[int] = field(default_factory=set)
    _rev: int = 0
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _assistant: int | None = None
    _approvals: dict[str, _Approval] = field(default_factory=dict)
    _inbox_seen: int = 0
    _interrupt_seen: int = 0
    _started: int = 0
    # Set by the REPL: True while a turn is running, so a message arriving now
    # is queued rather than answered.
    waiting: bool = False
    _head_chunks: int = -1
    _last_activity: float = 0.0

    @property
    def active(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> Link:
        if self.active and self.link:
            return self.link
        self._stack = AsyncExitStack()
        self._client = await self._stack.enter_async_context(connect(self.config))
        self.link = await self._ensure_publication()
        self.sid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        self._started = _now_ms()
        self.events.clear()
        self._dirty.clear()
        self._assistant = None
        self._inbox_seen = 0
        self._interrupt_seen = 0
        self.interrupt_requested.clear()
        await self._write_head(state="live")
        self._tasks = [
            asyncio.create_task(self._writer(), name="scivo-control-writer"),
            asyncio.create_task(self._poller(), name="scivo-control-poller"),
        ]
        self.status("connected — this session is now driven from here as well as the terminal")
        return self.link

    async def put_commands(self, commands: list[dict[str, str]]) -> None:
        """The slash commands the page offers in its menu, once per session."""
        await self._call("put_page_data", pub_id=self.link.pub_id, collection="content",
                         doc_id=f"commands-{self.sid}", data={"sid": self.sid, "list": commands})

    def note(self, markdown: str) -> None:
        """Output of a local command, shown on the page as rendered markdown."""
        if self.active:
            self.end_assistant()
            self._append({"kind": "assistant", "text": markdown, "done": True})

    async def stop(self) -> None:
        if not self.active:
            return
        self.status("disconnected — the terminal has taken the session back", level="warn")
        for pending in self._approvals.values():
            if not pending.future.done():
                pending.future.set_result("deny")
        try:
            await self._flush()
            await self._write_head(state="ended")
            await self._call("update_publication", pub_id=self.link.pub_id, active=False)
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
            if self._stack:
                await self._stack.aclose()
            self._client = None
            self._stack = None

    async def _ensure_publication(self) -> Link:
        """Reuse this project's control page if it still exists, else publish one.

        Reuse keeps the passcode stable, which is what makes it usable from a
        phone. The page is re-uploaded every time so a harness update reaches it.
        """
        state_path = self.config.root / STATE_FILE
        stored: dict[str, Any] = {}
        if state_path.is_file():
            try:
                stored = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                stored = {}

        html = page_html()
        if stored.get("pub_id") and stored.get("passcode"):
            outcome = await self._call("update_publication", pub_id=stored["pub_id"],
                                       html=html, active=True, require_passcode=True,
                                       kind="control")
            if outcome.ok:
                return Link(stored["pub_id"], stored["url"], stored["passcode"])

        published = await self._call(
            "publish_page",
            title=f"scivo-control · {self.project_name}",
            description="Drive a local scivo session from the web. Owner only.",
            html=html,
            require_passcode=True,
            # The dashboard knows this kind: it docks the page in the project's
            # corner and keeps it out of the Published list, where a link that
            # drives someone's terminal does not belong.
            kind="control",
        )
        if not published.ok or not isinstance(published.first, dict):
            raise RuntimeError(f"could not publish the control page: {published.error}")
        pub = published.first
        code = await self._call("add_passcode", pub_id=pub["pub_id"], label=OWNER)
        if not code.ok or not isinstance(code.first, dict):
            raise RuntimeError(f"could not issue the owner passcode: {code.error}")

        link = Link(pub["pub_id"], pub["url"], code.first["passcode"])
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(link.to_json(), indent=2) + "\n", encoding="utf-8")
        state_path.chmod(0o600)  # the passcode is a credential for this machine
        return link

    async def _call(self, name: str, **arguments: Any):
        async with self._lock:
            return await self._client.call(name, **arguments)

    # ------------------------------------------------------- outbound events

    def _append(self, event: dict[str, Any]) -> int:
        index = len(self.events)
        event = {"i": index, "t": _now_ms(), **event}
        if isinstance(event.get("text"), str) and len(event["text"]) > MAX_TEXT:
            event["text"] = event["text"][:MAX_TEXT] + "\n\n…[truncated for the web view]"
        self.events.append(event)
        self._touch(index)
        return index

    def _touch(self, index: int) -> None:
        self._dirty.add(index // CHUNK)
        self._last_activity = time.monotonic()
        self._wake.set()

    def user(self, text: str, via: str) -> None:
        if self.active:
            self.end_assistant()
            self._append({"kind": "user", "text": text, "via": via})

    def delta(self, text: str) -> None:
        if not self.active or not text:
            return
        if self._assistant is None:
            self._assistant = self._append({"kind": "assistant", "text": "", "done": False})
        event = self.events[self._assistant]
        if len(event["text"]) < MAX_TEXT:
            event["text"] += text
        self._touch(self._assistant)

    def end_assistant(self) -> None:
        if self._assistant is not None:
            self.events[self._assistant]["done"] = True
            self._touch(self._assistant)
            self._assistant = None

    def tool(self, name: str, detail: str) -> None:
        if self.active:
            self.end_assistant()
            self._append({"kind": "tool", "name": name, "detail": detail})

    def result(self, turns: int, cost: float, total: float, error: str | None = None) -> None:
        if self.active:
            self.end_assistant()
            self._append({"kind": "result", "turns": turns, "cost": round(cost, 4),
                          "total": round(total, 4), "error": error})

    def status(self, text: str, level: str = "info") -> None:
        if self.active:
            self.end_assistant()
            self._append({"kind": "status", "text": text, "level": level})

    async def ask_approval(self, tool: str, detail: str, outward: bool) -> str:
        """Post an approval card and wait for the owner's decision on the page."""
        self.end_assistant()
        rid = secrets.token_hex(6)
        index = self._append({"kind": "approval", "rid": rid, "tool": tool, "detail": detail,
                              "outward": outward, "state": "pending"})
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._approvals[rid] = _Approval(rid, index, future)
        try:
            decision = await future
        finally:
            self._approvals.pop(rid, None)
        if outward and decision == "always":
            decision = "allow"  # never remembered for tools that reach outside
        self.events[index]["state"] = decision
        self._touch(index)
        return decision

    # ------------------------------------------------------------- the loops

    async def _write_head(self, state: str) -> None:
        self._rev += 1
        self._head_chunks = (len(self.events) - 1) // CHUNK if self.events else -1
        await self._call("put_page_data", pub_id=self.link.pub_id, collection="content",
                         doc_id="head", data={
                             "sid": self.sid, "rev": self._rev, "state": state,
                             "events": len(self.events), "chunk": CHUNK,
                             "beat": _now_ms(), "started": self._started,
                             "project": self.project_name, "project_id": self.project_id,
                             "model": self.model,
                         })

    async def _flush(self) -> bool:
        if not self._dirty:
            return False
        chunks, self._dirty = sorted(self._dirty), set()
        for k in chunks:
            outcome = await self._call(
                "put_page_data", pub_id=self.link.pub_id, collection="content",
                doc_id=f"log-{self.sid}-{k:04d}",
                data={"sid": self.sid, "k": k, "events": self.events[k * CHUNK:(k + 1) * CHUNK]},
            )
            if not outcome.ok:
                self.errors.append(f"write chunk {k}: {outcome.error}")
                self._dirty.add(k)  # try again next round
        return True

    async def _writer(self) -> None:
        last_beat = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=BEAT_EVERY)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            await asyncio.sleep(FLUSH_EVERY)  # let a burst of deltas coalesce
            await self._flush()
            # The page subscribes to the chunks themselves, so the head only has
            # to move when a new chunk begins — that is how the page learns to
            # listen to it — or when the heartbeat is due. Writing it on every
            # flush doubled the calls on a server that answers one at a time.
            chunks_now = (len(self.events) - 1) // CHUNK if self.events else -1
            if chunks_now != self._head_chunks or time.monotonic() - last_beat >= BEAT_EVERY:
                await self._write_head(state="live")
                last_beat = time.monotonic()

    async def _poller(self) -> None:
        while True:
            busy = (time.monotonic() - self._last_activity < ACTIVE_WINDOW
                    or any(not a.future.done() for a in self._approvals.values()))
            await asyncio.sleep(POLL_ACTIVE if busy else POLL_IDLE)
            outcome = await self._call("list_responses", pub_id=self.link.pub_id)
            if not outcome.ok:
                self.errors.append(f"poll: {outcome.error}")
                continue
            for doc in outcome.items:
                if not isinstance(doc, dict):
                    continue
                # The label is stamped by the server from the passcode, not
                # taken from the page, so this is the one check that cannot be
                # spoofed from page code.
                if doc.get("reviewer") != OWNER or doc.get("sid") != self.sid:
                    continue
                kind = doc.get("doc")
                if kind == "inbox":
                    for message in sorted(doc.get("msgs") or [], key=lambda m: m.get("seq", 0)):
                        seq = int(message.get("seq", 0))
                        if seq > self._inbox_seen and str(message.get("text", "")).strip():
                            self._inbox_seen = seq
                            self._last_activity = time.monotonic()
                            text = str(message["text"])
                            # Echo it here rather than when the session gets
                            # round to it: a message sent while a turn is
                            # running sat as "sending…" on the page for as long
                            # as the turn took, which reads as a lost message.
                            self.user(text, "web")
                            if self.waiting:
                                self.status("queued — the session is busy with the previous turn")
                            await self.messages.put(text)
                elif kind == "approvals":
                    for rid, decision in (doc.get("decisions") or {}).items():
                        pending = self._approvals.get(rid)
                        if pending and not pending.future.done() and decision in {"allow", "always", "deny"}:
                            pending.future.set_result(decision)
                elif kind == "control":
                    seq = int(doc.get("interrupt") or 0)
                    if seq > self._interrupt_seen:
                        self._interrupt_seen = seq
                        self.interrupt_requested.set()
