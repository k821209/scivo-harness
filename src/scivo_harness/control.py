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

Scope is one person driving their own session. The dashboard signs the project
owner in to a `kind="control"` page from their own login and stamps every
response `reviewer: "owner"`; this side acts on nothing else. No passcode is
minted, so there is no code to forward — routing someone else's prompts through
this machine's Claude login is not something to do quietly.
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
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}
# Steps tried in order until one fits the cap. A single 1400 px pass was the
# whole ladder, so anything that did not fit after it was dropped.
SHRINK_LADDER = ((1400, 85), (1100, 80), (900, 75), (700, 70))
# Further steps, taken only when the image has to fit what is LEFT of a
# document rather than the per-image cap. Not part of the ladder above: with
# these in it, a half-transparent photograph found a 500 px PNG that "fit" and
# kept its alpha at a third of the size, where flattening it at 1400 px is the
# better picture.
TIGHT_RUNGS = ((500, 65), (360, 60))
MAX_IMG_DIMENSION = SHRINK_LADDER[0][0]
# Per image, after downscale. Base64 inflates by a third and the result is
# written inside a transcript chunk document, which Firestore caps at 1 MB —
# so this has to leave room for that expansion and for the other events in the
# same document. 800 KB did not: 800 KB of image is 1.07 MB of base64.
MAX_IMG_BYTES = 500_000
MAX_INLINE_BYTES = 1_500_000 # per assistant message
# What ONE transcript chunk document may weigh, serialized. Firestore refuses a
# document over 1,048,576 bytes, and the caps above are per image and per
# message while the thing with the limit is the DOCUMENT: twenty events that
# share it. Two pictures in one reply — each under its own cap — made a
# 1.2 MB chunk that could never be written; it was retried for the rest of the
# session, everything else in it never reached the page, and the writer's
# backoff, which only resets when nothing is waiting, pinned every later
# update to a 30-second cadence (review 2026-11-10, reproduced). Under the
# limit by a margin, because JSON bytes only approximate Firestore's count.
CHUNK_DOC_BUDGET = 900_000
# When the inbox is READ rather than listened to, "new" means a `seq` above
# the highest taken in — and `seq` is the browser's clock. A phone a minute
# behind the desktop writes a message that is "older" than one already seen,
# and it was never returned: sent, shown as sending, lost. So the read asks
# from this far BELOW the highest, and the ids already taken in keep that
# from delivering anything twice. A device more than this far out still
# loses; the listener, which counts for itself, never does.
SEQ_SKEW_MS = 10 * 60 * 1000
_DATA_IMG = None           # compiled lazily, once
_PIL_MISSING_NOTED = False
_MD_IMG = None             # compiled lazily, once


def _inline_local_images(text: str, project_root: Path,
                         extra_roots: tuple[str, ...] = (),
                         room: int | None = None) -> str:
    """Rewrite `![alt](local/path.png)` to a data URI so the page can show it.

    The page runs on an https origin and cannot fetch file:// paths, so
    without this a screenshot the model referenced was just a broken image
    icon. Cap per image and per message so a big render cannot break the doc.

    `extra_roots` is the session's `--add-dir` list. A render pipeline writes
    outside the project — a ComfyUI output directory, a scratch dir — and
    those paths were refused, so the one image somebody actually wanted to see
    was the one that stayed a broken reference. The containment rule itself
    stays: these are directories the USER named on the command line, not paths
    taken from model text.

    `room` is how many bytes of data URI this text may gain — what is left in
    the document it will be written into. Each image is shrunk to fit what
    remains, so a second picture arrives smaller rather than not at all; one
    that cannot be made to fit keeps its plain reference and says why.
    """
    global _MD_IMG
    if _MD_IMG is None:
        import re
        _MD_IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
    if "![" not in text:
        return text
    import base64
    added = 0
    spent = 0
    roots = []
    for candidate in (project_root, *extra_roots):
        try:
            roots.append(Path(candidate).expanduser().resolve())
        except (OSError, ValueError):
            continue

    def sub(match):
        nonlocal added, spent
        alt, ref = match.group(1), match.group(2).strip()
        if "://" in ref or ref.startswith("data:") or ref.startswith("#"):
            return match.group(0)
        try:
            path = Path(ref).expanduser()
            if not path.is_absolute():
                path = project_root / path
            path = path.resolve()
        except (OSError, ValueError):
            return match.group(0)
        # Only files under the project or a directory the user added. The
        # reference is model text, and the upload it triggers goes through no
        # approval — an injected `![x](../../.ssh/id_rsa.png)` would otherwise
        # ship any image on the machine to the page.
        if not any(_within(path, root) for root in roots):
            return match.group(0)
        if not path.is_file():
            return match.group(0)
        mime = IMAGE_TYPES.get(path.suffix.lower())
        if mime is None:
            return match.group(0)
        try:
            data = path.read_bytes()
        except OSError:
            return match.group(0)
        original = len(data)
        cap = MAX_IMG_BYTES
        if room is not None:
            # Base64 is 4 bytes for every 3; 120 is the markdown around it.
            cap = min(cap, max(0, (room - spent - 120) * 3 // 4))
        data, mime, note = _shrink_for_web(data, mime, path.suffix.lower(), cap)
        if len(data) > cap or added + len(data) > MAX_INLINE_BYTES:
            hint = "" if note is None else f" — {note}"
            if cap < MAX_IMG_BYTES and len(data) <= MAX_IMG_BYTES:
                # It would have fitted on its own: say what took the room, or
                # this reads as the image being too big, which it is not.
                hint += (" — this stretch of the conversation already holds "
                         "as much as one page document can; ask for it again "
                         "to see it")
            return (f"{match.group(0)}\n\n_[{path.name}: {original // 1024} KB — "
                    f"not inlined for the web view{hint}]_")
        added += len(data)
        caption = f"{alt}" if alt else path.name
        uri = f"![{caption}](data:{mime};base64,{base64.b64encode(data).decode('ascii')})"
        spent += len(uri)
        return uri

    return _MD_IMG.sub(sub, text)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _shrink_for_web(data: bytes, mime: str, suffix: str,
                    cap: int = MAX_IMG_BYTES) -> tuple[bytes, str, str | None]:
    """Downscale a big image so it fits the chat, using Pillow when installed.

    A camera-sized JPG blew past every cap and came back to the user as a
    "not inlined" note. Pillow is not required, but if it is around we resize
    to a web-friendly width and re-encode; without it, big images stay big
    and skip inlining, with a note saying how to enable it.

    Two things decide whether it fits, and both used to be wrong for the same
    picture — a photograph saved as PNG (user, 2026-10-10, 1,638 KB shown as
    "not inlined"):

    - **The output format follows the image, not the filename.** Re-encoding a
      photograph as PNG because its name ends in `.png` kept it at 1.4 MB;
      the same picture as JPEG is 295 KB. PNG is kept only when the image
      actually carries transparency, which is the thing JPEG cannot do.
    - **One pass is not an answer.** If 1400 px still does not fit, step down
      and try again instead of giving up.

    `cap` is what "fits" means for this call. The default is the per-image
    ceiling; a caller writing into a document that is already partly full
    passes what is left of it.
    """
    global _PIL_MISSING_NOTED
    if mime == "image/svg+xml":
        return data, mime, None
    if len(data) <= cap and cap < MAX_IMG_BYTES:
        return data, mime, None      # already fits the room there is
    try:
        from PIL import Image
        import io as _io
    except ImportError:
        if not _PIL_MISSING_NOTED:
            _PIL_MISSING_NOTED = True
            return data, mime, "install Pillow to auto-downscale"
        return data, mime, None
    try:
        img = Image.open(_io.BytesIO(data))
        img.load()
        # Transparency is the only reason to pay PNG's size for a photograph.
        keeps_alpha = (img.mode in ("RGBA", "LA")
                       or (img.mode == "P" and "transparency" in img.info))
        best: bytes | None = None
        best_mime = mime

        def encode(dimension: int, quality: int, alpha: bool) -> tuple[bytes, str]:
            frame = img
            w, h = img.size
            if max(w, h) > dimension:
                scale = dimension / max(w, h)
                frame = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                   Image.LANCZOS)
            buf = _io.BytesIO()
            if alpha:
                frame.save(buf, "PNG", optimize=True)
                return buf.getvalue(), "image/png"
            flat = frame
            if frame.mode in ("RGBA", "LA", "P"):
                # Transparency onto white: a chat bubble has a light background
                # and a picture that shows beats one that does not.
                flat = Image.new("RGB", frame.size, (255, 255, 255))
                rgba = frame.convert("RGBA")
                flat.paste(rgba, mask=rgba.split()[-1])
            flat.convert("RGB").save(buf, "JPEG", quality=quality,
                                     optimize=True, progressive=True)
            return buf.getvalue(), "image/jpeg"

        # Transparency first when the image has it; then, only if nothing fit,
        # the same ladder flattened onto white.
        passes = (True, False) if keeps_alpha else (False,)
        for alpha in passes:
            ladder = SHRINK_LADDER if cap >= MAX_IMG_BYTES else SHRINK_LADDER + TIGHT_RUNGS
            for dimension, quality in ladder:
                candidate, out_mime = encode(dimension, quality, alpha)
                if best is None or len(candidate) < len(best):
                    best, best_mime = candidate, out_mime
                if len(candidate) <= cap:
                    return candidate, out_mime, None
    except Exception:  # noqa: BLE001 - a corrupt or exotic image should not break the turn
        return data, mime, None
    # Nothing fit. Hand back whichever is smaller; the caller decides what to
    # say about it.
    if best is not None and len(best) < len(data):
        return best, best_mime, None
    return data, mime, None


FLUSH_EVERY = 0.03         # seconds; streaming deltas coalesce into one write
POLL_ACTIVE = 0.15         # seconds between reads of web input while the session is busy
POLL_IDLE = 0.5            # … and once it has been quiet for ACTIVE_WINDOW
ACTIVE_WINDOW = 60.0       # seconds of quiet before polling slows down
# How often the inbox is read, by how long NOTHING has happened in the session.
#
# It used to stop at the second rung: half a second, for as long as the
# process lived. Measured on 2026-11-10 (Cloud Monitoring, 24 h): 34.1 million
# Firestore reads a day, and 22 control pages live — their median uptime a
# WEEK, the oldest 41 days — each asking twice a second whether anyone had
# typed. A poll is six billed reads (the publication, four fixed documents
# whether or not they exist, and a query that is charged one read for
# returning nothing), so an idle session cost a million reads a day to learn
# nothing. Cost has to follow use, and what an unattended session uses is
# none.
#
# The price of the lower rungs is latency on the FIRST web message after a
# long quiet: up to the interval. Typing in the terminal is activity and
# resets it at once; someone opening the page announces themselves (see
# VIEWER_FLOOR) so the wait is spent while they are still reading; and the
# page is told the interval, so "sending…" can say what it is waiting for.
POLL_LADDER = (
    (0.0, POLL_ACTIVE),
    (ACTIVE_WINDOW, POLL_IDLE),
    (600.0, 2.0),
    (3600.0, 15.0),
)
# While an approval or a question is waiting, never slower than this. It used
# to be the FASTEST state — 0.15 s for as long as the card sat there — so a
# card left overnight was the most expensive thing a session could do: forty
# reads a second, waiting for a person.
PENDING_FLOOR = 5.0
# Someone has the page open and is using it (the page says so, see `viewer`
# in the control document): not slower than this, so their message is not the
# thing that pays the wait. Not POLL_ACTIVE — a tab left open would otherwise
# hold a session at full cadence, which is the bill this ladder exists to stop.
VIEWER_FLOOR = 1.0
VIEWER_WINDOW = 90.0       # seconds a viewer's last sign of life counts for


# ── being told, instead of asking ───────────────────────────────────────────
# The ladder above makes asking cheaper. This makes it unnecessary: an MCP
# from 2026-11-10 on has `wait_for_responses`, which holds a Firestore
# listener open and answers when the page WRITES — billed for the documents
# that change, nothing while none do, and delivered in a tenth of a second
# rather than at the next tick. Measured through the real server: a write
# reached this side in 67 ms, and an idle wait read nothing.
LISTEN_WAIT = 25.0         # seconds one call is held open
# While the listener is delivering, the inbox is still READ this often. Not
# for speed — as the check on the listener itself: a stream can stop without
# raising anywhere, and a listener that is trusted because it is quiet is the
# failure this project keeps finding. Ten minutes is 144 reads of six a day.
RECONCILE_EVERY = 600.0
# A healthy listener answers at least once per LISTEN_WAIT. Past this without
# an answer it is not healthy, whatever it last said.
LISTEN_STALE = LISTEN_WAIT + 30.0
# How many times the reconcile read may find something the listener never
# delivered before the listener is no longer believed.
LISTEN_MISSES_ALLOWED = 1


def _is_unknown_tool(error: Any) -> bool:
    """The server saying it has no such tool — and nothing else. "publication
    not found" also arrives as a tool error; reading that as "no such tool"
    would switch the listener off for good over a page that was unpublished."""
    return str(error or "").lstrip().lower().startswith("unknown tool")


def poll_interval(quiet: float, *, pending: bool = False, viewer: bool = False) -> float:
    """Seconds until the next read of the inbox."""
    interval = POLL_LADDER[0][1]
    for after, seconds in POLL_LADDER:
        if quiet >= after:
            interval = seconds
    if pending:
        interval = min(interval, PENDING_FLOOR)
    if viewer:
        interval = min(interval, VIEWER_FLOOR)
    return interval
BEAT_EVERY = 5.0           # seconds; the page shows "disconnected" past a few misses
# …while someone can see it. The heartbeat exists so a session that DIED
# (killed, the machine gone — it cannot say goodbye) stops looking alive, and
# five seconds is how fast a person watching should find out. Nobody is
# watching a session that has sat idle for a week, and it was writing 17,280
# heartbeats a day regardless: 22 sessions, 420,000 writes a day, and each
# one a billed read for every browser tab subscribed to the head (measured
# 2026-11-10). Unwatched and idle, it now beats this often — and the head
# says which, so the dashboard judges "connected" against the interval the
# session is actually keeping rather than one it has stopped keeping.
BEAT_IDLE = 300.0
# Every call the page makes is bounded. `_call` holds `self._lock`, so one that
# never returns used to hold it forever: the writer stopped flushing, the
# poller stopped reading, the heartbeat stopped — the page froze solid while
# the terminal carried on as if nothing were wrong (user, 2026-11-08: "scivo
# 웹채팅에서는 멈춰있고 cmd에서는 [정상]"). A hang is now an error, which is a
# thing that can be reported, instead of silence.
CALL_TIMEOUT = 45.0
# How long the page may be behind before the terminal is told. Longer than a
# couple of write cycles, so an ordinary burst does not raise it.
STUCK_AFTER = 60.0
STATE_FILE = Path(".scivo") / "control.json"

# ── one session drives a page at a time ─────────────────────────────────────
# A project has ONE control page, and nothing stopped a second session in the
# same project from starting on it: it deleted the first session's transcript
# ("clear previous sessions" — the first was not previous, it was running),
# and from then on the two took turns writing the head, so the page showed
# one conversation, then the other, every few seconds (review 2026-11-10,
# reproduced). The session that owns the page now says so in a small document
# of its own, and a newcomer reads it before touching anything.
LEASE_COLLECTION = "lease"          # readable in one cheap call; `content` is not
LEASE_DOC = "owner"
LEASE_EVERY = 60.0                  # seconds between renewals while nothing asks
LEASE_ANSWER_WAIT = 4.0             # how long a holder is given to answer "are you there?"


class PageInUse(RuntimeError):
    """Another live session is driving this project's page."""


def page_html() -> str:
    return resources.files("scivo_harness").joinpath("control_page.html").read_text(encoding="utf-8")


def merge_web_messages(items: list) -> Any:
    """Fold everything the page sent while a turn was running into one message.

    The queue hands out one message per prompt, so a second line typed while
    the session was busy waited for the turn AFTER the one it was queued for —
    and the second line is usually the correction to the first ("아니지 …")
    (user, 2026-10-10). Sent together, they read as what they are: one thought,
    revised.

    Text joins with a blank line between sends; images accumulate. A single
    item is returned unchanged, so the common path is untouched.
    """
    if len(items) <= 1:
        return items[0] if items else ""
    texts: list[str] = []
    images: list = []
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            images.extend(item.get("images") or [])
        else:
            text = str(item or "").strip()
        if text:
            texts.append(text)
    joined = "\n\n".join(texts)
    if images:
        return {"text": joined, "images": images}
    return joined


def _rejects_since_seq(error: Any) -> bool:
    """Did this failure come from the server not knowing the argument?"""
    text = str(error or "").lower()
    return "since_seq" in text and any(
        marker in text for marker in
        ("unexpected", "unknown", "extra", "not permitted", "validation", "invalid"))


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Link:
    pub_id: str
    url: str

    def to_json(self) -> dict[str, str]:
        return {"pub_id": self.pub_id, "url": self.url}


@dataclass
class _Approval:
    rid: str
    index: int
    future: asyncio.Future


@dataclass
class _Question:
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
    # The session's `--add-dir` directories: image references under them inline
    # as well as ones under the project.
    extra_roots: tuple[str, ...] = ()

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
    # When a flush last got everything through. None until the first one: a
    # page that never wrote and a page that stopped writing are not the same
    # state and must not read the same.
    _last_flush: float | None = None
    _stuck_said: bool = False
    # Where the session is editing right now, for the dashboard to draw a
    # caret on. Written through the writer loop rather than as its own task:
    # that loop is already bounded and backed off, and a caret is not worth a
    # second path to the network.
    _focus: dict[str, Any] | None = None
    _focus_dirty: bool = False
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _assistant: int | None = None
    _approvals: dict[str, _Approval] = field(default_factory=dict)
    _questions_pending: dict[str, _Question] = field(default_factory=dict)
    _inbox_seen: int = 0
    _inbox_ids: set = field(default_factory=set)
    # The highest `seq` already taken in. Sent with every poll so the server
    # returns the documents it rewrites in place plus only NEW messages: this
    # poll used to fetch every message of the session, twice a second, to
    # discover nothing — 99 documents a tick on a long-running session, and
    # 98% of the project's Firestore bill (user, 2026-10-10). The conversation
    # is not in those documents; they are a delivery queue and this side
    # already holds what it has taken in.
    _inbox_watermark: int = 0
    # Every scivo tool refuses arguments it does not know, so an MCP older
    # than `since_seq` would fail EVERY poll and the inbox would go silent.
    # Detected once, from the first failure, and never sent again.
    _since_seq_supported: bool = True
    _interrupt_seen: int = 0
    # The page's own "someone is here" mark, and when (on OUR clock) it last
    # changed. Compared for change, never against the wall clock: it is
    # stamped by a browser, and a phone whose clock is a minute out must not
    # read as absent — or as permanently present.
    _viewer_mark: Any = None
    _viewer_at: float = 0.0
    _poll_s: float = POLL_ACTIVE
    # Set when something happens HERE while the poller is in one of its long
    # sleeps, so a session that has just woken — a line typed in the terminal,
    # an approval card posted — is not deaf to the page for the rest of a
    # fifteen-second nap it began while idle.
    _poll_wake: asyncio.Event = field(default_factory=asyncio.Event)
    # The listener: whether this MCP has one, where we are in its stream, and
    # when it last answered. `_listen_trusted` goes False for the rest of the
    # session if the reconcile read keeps finding what it never delivered.
    _listen_supported: bool = True
    _listen_trusted: bool = True
    _listen_cursor: str = ""
    _listen_at: float = 0.0
    _listen_misses: int = 0
    _listen_why: str = ""
    _started: int = 0
    # Set by the REPL: True while a turn is running, so a message arriving now
    # is queued rather than answered.
    waiting: bool = False
    _head_chunks: int = -1
    # What the head last told the page about our own rhythm. A change in
    # either is written at once: a head still promising a beat every five
    # seconds, from a session that has gone to five minutes, reads as dead.
    _head_beat_every: float = BEAT_EVERY
    _head_hint: bool = False
    # The lease: when it was last renewed, and whether something (a viewer, a
    # newcomer asking "are you there?") wants it renewed now.
    _lease_at: float = 0.0
    _lease_due: bool = False
    _claim_note: str = ""
    # Set when another session took the page: this one has stopped writing to
    # it and says so, instead of fighting over the head.
    taken_by: str = ""
    _last_activity: float = 0.0

    @property
    def active(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------ lifecycle

    async def start(self, take: bool = False) -> Link:
        """Publish (or reuse) the page and start driving it.

        `take` is the person saying "this session, not the one that has it":
        without it a page another live session is driving is left alone and
        `PageInUse` says who has it.
        """
        if self.active and self.link:
            return self.link
        self._stack = AsyncExitStack()
        self._client = await self._stack.enter_async_context(connect(self.config))
        self.sid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        self.taken_by = ""
        try:
            self.link = await self._ensure_publication(take=take)
        except BaseException:
            # Nothing was started, so nothing may be left looking started: a
            # Control that failed here used to stay `active` with no tasks and
            # an MCP process nobody would close.
            stack, self._stack, self._client = self._stack, None, None
            await stack.aclose()
            raise
        self._started = _now_ms()
        self.events.clear()
        self._dirty.clear()
        self._assistant = None
        self._inbox_seen = 0
        self._inbox_ids = set()
        self._inbox_watermark = 0
        self._since_seq_supported = True
        self._interrupt_seen = 0
        self._viewer_mark = None
        self._viewer_at = 0.0
        self._listen_supported = True
        self._listen_trusted = True
        self._listen_cursor = ""
        self._listen_at = 0.0
        self._listen_misses = 0
        self._listen_why = ""
        self._last_activity = time.monotonic()     # a session that just started is not idle
        self.interrupt_requested.clear()
        await self._write_lease()
        await self._write_head(state="live")
        self._tasks = [
            asyncio.create_task(self._writer(), name="scivo-control-writer"),
            asyncio.create_task(self._poller(), name="scivo-control-poller"),
            asyncio.create_task(self._listener(), name="scivo-control-listener"),
        ]
        self.status("connected — this session is now driven from here as well as the terminal")
        return self.link

    async def put_commands(self, commands: list[dict[str, str]]) -> None:
        """The slash commands the page offers in its menu, once per session."""
        await self._call("put_page_data", pub_id=self.link.pub_id, collection="content",
                         doc_id=f"commands-{self.sid}", data={"sid": self.sid, "list": commands})

    def focus(self, slug: str, section: str, block_id: str | None,
              tool: str) -> None:
        """Say which paragraph the session is working on.

        The dashboard's Paper tab draws a marker on it — the one thing another
        editor had that ours did not: you could see where the agent was
        (user, 2026-11-09). Carries `at` so the reader can decide it is stale
        rather than needing a second write to clear it; a session that dies
        mid-edit would never send that clear.
        """
        if not self.active or not slug:
            return
        self._focus = {"doc": "focus", "sid": self.sid, "slug": slug,
                       "section": section or "", "block_id": block_id or "",
                       "tool": tool, "at": _now_ms()}
        self._focus_dirty = True
        self._wake.set()

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
        for pending in self._questions_pending.values():
            if not pending.future.done():
                pending.future.set_result(None)
        try:
            await self._flush()
            await self._write_head(state="ended")
            # Given up on the way out, so the next session — the same one after
            # `/update`, usually — is not made to wait for it to run out.
            await self._call("clear_page_data", pub_id=self.link.pub_id,
                             collection=LEASE_COLLECTION, doc_id=LEASE_DOC)
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

    # ------------------------------------------------------------- the lease

    async def _read_lease(self, pub_id: str) -> dict | None:
        outcome = await self._call("list_responses", pub_id=pub_id, collection=LEASE_COLLECTION)
        if not outcome.ok:
            return None
        for row in outcome.items:
            if isinstance(row, dict) and row.get("id") == LEASE_DOC and row.get("sid"):
                return row
        return None

    async def _write_lease(self) -> None:
        import os
        import socket

        self._lease_at = time.monotonic()
        self._lease_due = False
        await self._call("put_page_data", pub_id=self.link.pub_id,
                         collection=LEASE_COLLECTION, doc_id=LEASE_DOC,
                         data={"sid": self.sid, "at": _now_ms(), "every": LEASE_EVERY,
                               "host": socket.gethostname(), "pid": os.getpid(),
                               "listening": self.listening, "project": self.project_name})

    async def _holder_alive(self, pub_id: str, lease: dict) -> tuple[bool, str]:
        """Is the session named in this lease still there? (verdict, why).

        Three questions, cheapest and surest first. The lease's own age: one
        long past its renewal is nobody's. This machine's process table, when
        the holder is on this machine: that answer is certain either way. And
        for a holder elsewhere, ASKING — the same mark the page writes when
        someone opens it, which a live session answers by renewing at once.
        """
        import os
        import socket

        age = (_now_ms() - int(lease.get("at") or 0)) / 1000
        every = float(lease.get("every") or LEASE_EVERY)
        if age > every * 2.5:
            return False, f"its lease ran out ({age:.0f}s old, renewed every {every:.0f}s)"
        if lease.get("host") == socket.gethostname() and lease.get("pid") == os.getpid():
            # This very process, before it re-executed itself (`/update` keeps
            # the pid). It gives the lease up on the way out; if that write
            # was lost, its own leftover must not lock it out of its page.
            return False, "it was this process before it restarted"
        if lease.get("host") == socket.gethostname() and lease.get("pid"):
            try:
                os.kill(int(lease["pid"]), 0)
            except ProcessLookupError:
                return False, f"its process ({lease['pid']}) is gone"
            except (PermissionError, ValueError, OverflowError):
                pass                         # exists, or unreadable: ask it instead
            else:
                return True, f"process {lease['pid']} on this machine"
        before = lease.get("at")
        # Its own document, not the holder's `control`: this write REPLACES
        # what is there, and that one carries the Stop counter a reloaded tab
        # resumes from. `seq` so a holder reading on a timer is handed it too.
        await self._call("put_page_data", pub_id=pub_id, collection="responses", doc_id="ping",
                         data={"doc": "control", "sid": lease["sid"], "reviewer": OWNER,
                               "viewer": _now_ms(), "seq": _now_ms()})
        deadline = time.monotonic() + LEASE_ANSWER_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            again = await self._read_lease(pub_id)
            if again is None or again.get("sid") != lease["sid"]:
                return False, "it gave the page up"
            if again.get("at") != before:
                return True, "it answered"
        if lease.get("listening"):
            return False, (f"it was listening for the page and did not answer in "
                           f"{LEASE_ANSWER_WAIT:.0f}s")
        return True, (f"it did not answer in {LEASE_ANSWER_WAIT:.0f}s, but it reads the page on "
                      f"a timer and renewed its lease {age:.0f}s ago — it may still be there")

    async def _claim(self, link: Link, take: bool) -> dict | None:
        """Refuse a page someone else is driving. Returns the lease that was
        displaced, if there was one, so its holder can be told."""
        lease = await self._read_lease(link.pub_id)
        if lease is None or lease.get("sid") == self.sid:
            return None
        alive, why = await self._holder_alive(link.pub_id, lease)
        if alive and not take:
            where = lease.get("host") or "another machine"
            raise PageInUse(
                f"another session is driving this project's page — {lease['sid']} on {where} "
                f"({why}).\n  Two sessions on one page take turns overwriting it, so this one "
                "has left it alone.\n  `/scivo-control take` moves the page to THIS session; "
                "the other keeps running in its terminal and is told.")
        self._claim_note = (f"took the page over from {lease['sid']}"
                            + ("" if alive else f" — {why}"))
        return lease

    async def _tell_displaced(self, link: Link, lease: dict) -> None:
        """Leave word for the session this one replaced. It reads its inbox
        like any other message; if it is gone, nobody reads this and the next
        start clears it."""
        await self._call("put_page_data", pub_id=link.pub_id, collection="responses",
                         doc_id="takeover",
                         data={"doc": "takeover", "sid": lease["sid"], "by": self.sid,
                               "reviewer": OWNER, "seq": _now_ms()})

    async def _yield_page(self, by: str) -> None:
        """Another session took the page: stop writing to it, and say so here.

        Not `stop()`: that marks the page ended and unpublishes it, which
        would be this session shutting down a page that is now someone
        else's.
        """
        if not self.active:
            return
        self.taken_by = by
        print(f"\n  [scivo-control] another session in this project ({by}) took the web page.\n"
              "  This session carries on here in the terminal and no longer writes to the page.\n"
              "  `/scivo-control take` brings it back.\n", flush=True)
        for pending in self._approvals.values():
            if not pending.future.done():
                pending.future.set_result("deny")
        for pending in self._questions_pending.values():
            if not pending.future.done():
                pending.future.set_result(None)
        current = asyncio.current_task()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            if task is not current:
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t is not current), return_exceptions=True)
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    async def _ensure_publication(self, take: bool = False) -> Link:
        """Reuse this project's control page if it still exists, else publish one.

        No passcode is issued. The dashboard signs the owner in to a
        `kind="control"` page from their own session — they are already logged
        in to reach the dock — and stamps `reviewer: "owner"`, which is the
        label every response here is checked against. `require_passcode` stays
        True with no codes minted, so the page opens for the owner and for
        nobody else: a visitor with the link has no code to present and none
        exists to be forwarded.
        """
        state_path = self.config.root / STATE_FILE
        stored: dict[str, Any] = {}
        if state_path.is_file():
            try:
                stored = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                stored = {}

        html = page_html()
        if stored.get("pub_id") and stored.get("url"):
            outcome = None
            for attempt in range(3):   # a transient failure used to orphan the live page
                outcome = await self._call("update_publication", pub_id=stored["pub_id"],
                                           html=html, active=True, require_passcode=True,
                                           kind="control")
                if outcome.ok:
                    break
                await asyncio.sleep(0.5 * (attempt + 1))
            if outcome is not None and outcome.ok:
                link = Link(stored["pub_id"], stored["url"])
                # BEFORE anything of an earlier session is cleared: "earlier"
                # was assumed, and the session being cleared was sometimes
                # still running.
                displaced = await self._claim(link, take)
                if stored.get("passcode"):
                    await self._retire_passcodes(link, state_path)
                await self._clear_previous_sessions(link)
                if displaced is not None:
                    await self._tell_displaced(link, displaced)
                return link
            # Publishing anew: close the old page first, or it stays active
            # with a stale head the owner may still have open.
            try:
                await self._call("update_publication", pub_id=stored["pub_id"], active=False)
            except Exception:  # noqa: BLE001
                pass

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
        link = Link(pub["pub_id"], pub["url"])
        self._remember(link, state_path)
        return link

    def _remember(self, link: Link, state_path: Path) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(link.to_json(), indent=2) + "\n", encoding="utf-8")
        state_path.chmod(0o600)

    async def _clear_previous_sessions(self, link: Link) -> None:
        """Drop the transcript chunks, command lists and responses of earlier
        sessions on this page. Nothing ever deleted them, so a reused page
        accumulated every past session's `log-<sid>-NNNN` forever. Best
        effort: an MCP older than 2026-09-17 has no clear_page_data, and the
        session runs fine without the tidy-up (the page keys on the sid)."""
        for collection in ("content", "responses"):
            try:
                await self._call("clear_page_data", pub_id=link.pub_id, collection=collection)
            except Exception:  # noqa: BLE001
                return

    async def _retire_passcodes(self, link: Link, state_path: Path) -> None:
        """Clear the codes issued before the dashboard could sign the owner in.

        A code that still exists is a code that can be forwarded, and this page
        drives a shell. Nothing needs it now, so nothing should keep it.
        """
        listed = await self._call("list_passcodes", pub_id=link.pub_id)
        for item in listed.items if listed.ok else []:
            code_id = item.get("code_id") if isinstance(item, dict) else None
            if code_id:
                await self._call("revoke_passcode", pub_id=link.pub_id, code_id=code_id)
        self._remember(link, state_path)

    async def _call(self, name: str, **arguments: Any):
        from .scivo_mcp import ToolOutcome
        try:
            async with self._lock:
                return await asyncio.wait_for(
                    self._client.call(name, **arguments), timeout=CALL_TIMEOUT)
        except asyncio.TimeoutError:
            note = f"{name}: no answer in {CALL_TIMEOUT:.0f}s"
            self.errors.append(note)
            return ToolOutcome(ok=False, error=note)

    def health(self) -> dict:
        """What the page's own machinery is doing — for a human to read.

        `errors` had six writers and no readers: a page that stopped updating
        said nothing anywhere, and the session it belongs to could not be asked
        why. This is the reader.
        """
        return {
            "active": self.active,
            "pending_chunks": len(self._dirty),
            "events": len(self.events),
            "errors": len(self.errors),
            "last_error": self.errors[-1] if self.errors else None,
            "last_flush_ago": (None if self._last_flush is None
                               else round(time.monotonic() - self._last_flush, 1)),
            # How the page's messages reach this side, and if not by being
            # told, why not — the difference is the whole Firestore bill.
            "inbox": "listening" if self.listening else f"read every {self._poll_s:g}s",
            "inbox_why": "" if self.listening else self._listen_why,
        }

    @property
    def listening(self) -> bool:
        """True while the listener is answering AND still believed."""
        return (self._listen_supported and self._listen_trusted and self._listen_at > 0
                and time.monotonic() - self._listen_at < LISTEN_STALE)

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
        if self._poll_s > POLL_IDLE:
            # Only out of a long sleep: at the short intervals the next read
            # is at most half a second away, and every streamed delta comes
            # through here.
            self._poll_wake.set()

    def user(self, text: str, via: str, images: list[str] | None = None) -> None:
        if self.active:
            self.end_assistant()
            event: dict[str, Any] = {"kind": "user", "text": text, "via": via}
            if images:
                event["images"] = images     # data URLs, already small
            self._append(event)

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
            event = self.events[self._assistant]
            event["text"] = _inline_local_images(
                event["text"], self.config.root, self.extra_roots,
                room=CHUNK_DOC_BUDGET - self._chunk_bytes(self._assistant // CHUNK))
            event["done"] = True
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

    async def ask_question(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Post the model's questions on the page and wait for the owner's answers.

        Returned as `{question_text: label_or_text}` or `{question_text: [labels]}`
        for multi-select, matching what Claude Code's AskUserQuestion tool checks
        against the option set. `None` means the page skipped it — the caller
        tells the model nobody answered.
        """
        self.end_assistant()
        rid = secrets.token_hex(6)
        # Deep-copy just enough that the page can render it without exposing
        # the harness's own dict to Firestore's non-JSON quirks.
        questions = [
            {"question": str(q.get("question", "")), "header": str(q.get("header", "")),
             "multiSelect": bool(q.get("multiSelect")),
             "options": [{"label": str(o.get("label", "")),
                          "description": str(o.get("description") or "")}
                         for o in (q.get("options") or []) if isinstance(o, dict)]}
            for q in (payload.get("questions") or []) if isinstance(q, dict)
        ]
        index = self._append({"kind": "question", "rid": rid, "questions": questions,
                              "state": "pending"})
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._questions_pending[rid] = _Question(rid, index, future)
        try:
            answers = await future
        finally:
            self._questions_pending.pop(rid, None)
        self.events[index]["state"] = "answered" if answers else "skipped"
        self.events[index]["answers"] = answers
        self._touch(index)
        return answers

    def _name_answers(self, pending: "_Question", answered: dict) -> dict:
        """Index-keyed answers from the page → `{question_text: value}`.

        `AskUserQuestion` checks answers against the option set by question
        text, so the text has to come back — but it must not travel as a map
        KEY: those become a Firestore property path, capped at 1500 bytes, and
        a long question made every click fail to send. The questions are on the
        event this card was posted from, so the mapping is free here.

        A key that is not an index passes through unchanged: a browser tab
        loaded before this change still sends question text, and an answer that
        arrives is worth more than a tidy shape.
        """
        try:
            questions = self.events[pending.index].get("questions") or []
        except (IndexError, KeyError, TypeError, AttributeError):
            questions = []
        out: dict = {}
        for key, value in answered.items():
            text = None
            if isinstance(key, str) and key.isdigit():
                position = int(key)
                if 0 <= position < len(questions):
                    text = str(questions[position].get("question") or "") or None
            out[text or key] = value
        return out

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
        beat_every = self._beat_every()
        self._head_beat_every = beat_every
        self._head_hint = self._poll_s >= 2
        await self._call("put_page_data", pub_id=self.link.pub_id, collection="content",
                         doc_id="head", data={
                             "sid": self.sid, "rev": self._rev, "state": state,
                             "events": len(self.events), "chunk": CHUNK,
                             "beat": _now_ms(), "started": self._started,
                             # Seconds until the next beat is due, so a reader
                             # knows how long silence is still normal.
                             "beat_every": beat_every,
                             # WHEN THIS CONVERSATION LAST HAD ANYTHING IN IT,
                             # which is not what `beat` says. `beat` is the
                             # heartbeat loop's own clock: it moves every few
                             # seconds for as long as the process lives, so a
                             # session idle since September and one being typed
                             # into right now carry beats seconds apart, and a
                             # list sorted by it cannot tell them apart (user,
                             # 2026-11-08: "소팅은 안되네"). The last event's
                             # timestamp is the thing a person means by "the
                             # most recent conversation".
                             "last_event_at": (self.events[-1].get("t")
                                               if self.events else None),
                             "project": self.project_name, "project_id": self.project_id,
                             "model": self.model,
                             # How long a message typed on the page may wait
                             # before this side reads it, so the page can say
                             # so instead of showing "sending…" at a session
                             # that is deliberately asleep.
                             "poll_s": self._poll_s,
                         })

    def _beat_every(self) -> float:
        """How often the head should beat right now.

        Fast while someone is using the page, or while anything is happening
        in the session (a turn running prints, and the Connect list sorts by
        what it last saw); slow otherwise.
        """
        now = time.monotonic()
        watched = self._viewer_at > 0 and now - self._viewer_at < VIEWER_WINDOW
        busy = now - self._last_activity < ACTIVE_WINDOW
        return BEAT_EVERY if (watched or busy) else BEAT_IDLE

    async def _flush(self) -> bool:
        if not self._dirty and not self._focus_dirty:
            return False
        if self._focus_dirty and self._focus is not None:
            # Before the chunks: the marker is only useful while the edit is
            # happening, and a burst of deltas must not put it behind them.
            self._focus_dirty = False
            outcome = await self._call("put_page_data", pub_id=self.link.pub_id,
                                       collection="content", doc_id="focus",
                                       data=self._focus)
            if not outcome.ok:
                self.errors.append(f"focus: {outcome.error}")
        chunks, self._dirty = sorted(self._dirty), set()
        for k in chunks:
            self._fit_chunk(k)
            outcome = await self._call(
                "put_page_data", pub_id=self.link.pub_id, collection="content",
                doc_id=f"log-{self.sid}-{k:04d}",
                data=self._chunk_doc(k),
            )
            if not outcome.ok:
                self.errors.append(f"write chunk {k}: {outcome.error}")
                self._dirty.add(k)  # try again next round
        if not self._dirty:
            self._last_flush = time.monotonic()
        return True

    def _chunk_doc(self, k: int) -> dict[str, Any]:
        return {"sid": self.sid, "k": k, "events": self.events[k * CHUNK:(k + 1) * CHUNK]}

    def _chunk_bytes(self, k: int) -> int:
        return len(json.dumps(self._chunk_doc(k), ensure_ascii=False).encode("utf-8"))

    def _fit_chunk(self, k: int) -> list[str]:
        """Make chunk `k` writable, whatever went into it. Returns what it cut.

        The invariant that was missing: a chunk handed to the network is under
        the document limit, ALWAYS. Inlining leaves room (`end_assistant`),
        but room is spent by whatever comes next into the same twenty events —
        a long reply, a picture pasted from the page — so the last word is
        here, at the one place every chunk passes through.

        Largest picture first, because one picture is what usually tips it and
        its reference text stays behind; then the longest text. Everything cut
        says so IN the event, since a page that silently shows less than the
        terminal did is the failure this file keeps having.
        """
        global _DATA_IMG
        if _DATA_IMG is None:
            import re
            _DATA_IMG = re.compile(r"!\[([^\]]*)\]\(data:image/[^)]+\)")
        cut: list[str] = []
        for _ in range(4 * CHUNK):                 # every step shrinks; bounded anyway
            over = self._chunk_bytes(k) - CHUNK_DOC_BUDGET
            if over <= 0:
                break
            events = self.events[k * CHUNK:(k + 1) * CHUNK]
            pictures = [(len(m.group(0)), event, m)
                        for event in events if isinstance(event.get("text"), str)
                        for m in _DATA_IMG.finditer(event["text"])]
            pasted = [(len(url), event, url) for event in events
                      for url in (event.get("images") or []) if isinstance(url, str)]
            if pictures and (not pasted or max(pictures)[0] >= max(pasted)[0]):
                size, event, m = max(pictures, key=lambda item: item[0])
                name = m.group(1) or "image"
                event["text"] = (event["text"][:m.start()]
                                 + f"_[{name}: not shown on the web view — this stretch "
                                   f"of the conversation outgrew one page document "
                                   f"({size // 1024} KB cut); ask for it again to see it]_"
                                 + event["text"][m.end():])
                cut.append(f"picture {name} ({size // 1024} KB)")
            elif pasted:
                size, event, url = max(pasted, key=lambda item: item[0])
                event["images"] = [u for u in event["images"] if u is not url]
                event["text"] = (str(event.get("text") or "")
                                 + f"\n\n_[a pasted image is not shown here "
                                   f"({size // 1024} KB) — the session still received it]_")
                cut.append(f"pasted image ({size // 1024} KB)")
            else:
                texts = [(len(event["text"].encode("utf-8")), event) for event in events
                         if isinstance(event.get("text"), str)]
                if not texts:
                    break                           # nothing here we know how to cut
                size, event = max(texts, key=lambda item: item[0])
                keep = max(0, size - over - 200)
                clipped = event["text"].encode("utf-8")[:keep].decode("utf-8", "ignore")
                event["text"] = clipped + "\n\n…[truncated for the web view]"
                cut.append(f"text ({(size - keep) // 1024} KB)")
        if cut:
            self.errors.append(f"chunk {k} was over one page document; cut: " + ", ".join(cut))
        return cut

    def _say_if_stuck(self) -> None:
        """Tell the TERMINAL when the page stops keeping up, once per spell.

        The terminal is the surface that still works when this one does not, so
        it is where the complaint belongs. Printed on the way in and on the way
        out: a warning with no all-clear trains people to ignore it.
        """
        stuck = bool(self._dirty) and (
            self._last_flush is None
            or time.monotonic() - self._last_flush > STUCK_AFTER)
        if stuck and not self._stuck_said:
            self._stuck_said = True
            last = self.errors[-1] if self.errors else "no error reported"
            print(f"\n  [scivo-control] the web page is not keeping up — "
                  f"{len(self._dirty)} chunk(s) waiting. Last: {last}\n"
                  f"  The terminal is unaffected. `/scivo-control` shows the "
                  f"detail; `/scivo-control off` then on republishes it.\n",
                  flush=True)
        elif self._stuck_said and not stuck:
            self._stuck_said = False
            print("\n  [scivo-control] the web page caught up.\n", flush=True)

    async def _writer(self) -> None:
        """Flush dirty chunks and keep the head's heartbeat alive.

        Wrapped in try/except so a transient MCP hiccup (a dropped stdio
        stream during a chunk write) does not leave the writer dead while
        deltas keep piling into `_dirty` and never reach the page.
        """
        last_beat = time.monotonic()
        stalls = 0
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._beat_every())
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            await asyncio.sleep(FLUSH_EVERY)  # let a burst of deltas coalesce
            failed = False
            try:
                await self._flush()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"flush: {type(exc).__name__}: {exc}")
                failed = True
            # A flush that left work behind has not got through either, even
            # though it raised nothing — that is what a refused or timed-out
            # chunk write looks like now that `_call` is bounded.
            stalls = (stalls + 1) if (failed or self._dirty) else 0
            if stalls:
                # Doubling, capped at half a minute. The link being away is
                # precisely when retrying at full cadence costs the most, and
                # a session can have ten of these loops open beside it.
                await asyncio.sleep(min(30.0, 0.5 * 2 ** min(stalls, 8)))
            self._say_if_stuck()
            # Drain anything that arrived while we were flushing, without
            # waiting again — otherwise the tail of a burst waits a full cycle.
            if self._dirty:
                self._wake.set()
            # The page subscribes to the chunks themselves, so the head only has
            # to move when a new chunk begins — that is how the page learns to
            # listen to it — or when the heartbeat is due. Writing it on every
            # flush doubled the calls on a server that answers one at a time.
            chunks_now = (len(self.events) - 1) // CHUNK if self.events else -1
            beat_every = self._beat_every()
            if (chunks_now != self._head_chunks
                    or time.monotonic() - last_beat >= beat_every
                    # Our rhythm changed, either way: slower must be said
                    # before the old promise runs out, faster is someone who
                    # has just arrived and is looking at the dot.
                    or beat_every != self._head_beat_every
                    or (self._poll_s >= 2) != self._head_hint
                    or self._lease_due):
                try:
                    await self._write_head(state="live")
                    # With the beat rather than on a timer of its own, and not
                    # with every beat: one renewal a minute keeps the lease,
                    # and a question gets its answer at once.
                    if self._lease_due or time.monotonic() - self._lease_at >= LEASE_EVERY:
                        await self._write_lease()
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"head: {type(exc).__name__}: {exc}")
                last_beat = time.monotonic()

    async def _take_web_message(self, text: str, images: list[str] | None = None) -> None:
        self._last_activity = time.monotonic()
        # Echo it here rather than when the session gets round to it: a
        # message sent while a turn is running sat as "sending…" on the page
        # for as long as the turn took, which reads as a lost message.
        urls = [u for u in (images or []) if isinstance(u, str) and u.startswith("data:image/")]
        self.user(text, "web", urls or None)
        if self.waiting:
            self.status("queued — the session is busy with the previous turn")
        if not urls:
            await self.messages.put(text)
            return
        from . import attachments
        paths: list[Path] = []
        for i, u in enumerate(urls):
            decoded = attachments.decode_data_url(u)
            if decoded:
                paths.append(attachments.save_inbound(self.config.root, decoded[0], decoded[1], f"web{i}"))
        await self.messages.put({"text": text, "images": paths})

    async def _intake(self, items: list) -> int:
        """Route what the page wrote: web messages, approvals, answers, Stop.

        Returns how many things it ACTED on — a message taken in, a card
        answered, a Stop. Zero for documents already dealt with, which is what
        lets one path (the reconcile read) check the other (the listener):
        anything it still finds to act on is something that was not delivered.
        """
        acted = 0
        # One document per web message (page ≥ 2026-09-28), taken in clock
        # order and deduplicated by id, so two tabs never race a counter.
        # `list_responses` stamps every row with `id` = its document id, so a
        # legacy one-list inbox arrives as id="inbox" and looked like a new-
        # style message with no text — dropped, and the page sat on
        # "sending…" (수요조사, 2026-09-30: a tab still running the old page).
        # The shape, not the id, tells the two apart: the old page writes
        # `msgs`, the new one `text`/`images`.
        fresh = sorted(
            (d for d in items if isinstance(d, dict) and d.get("doc") == "inbox"
             and "msgs" not in d
             and d.get("id") and d.get("reviewer") == OWNER and d.get("sid") == self.sid
             and d["id"] not in self._inbox_ids
             and (str(d.get("text", "")).strip() or d.get("images"))),
            key=lambda d: (d.get("seq") or 0, d["id"]))
        for d in fresh:
            self._inbox_ids.add(d["id"])
            await self._take_web_message(str(d.get("text") or ""), d.get("images"))
            acted += 1
        # Raised from every inbox doc this tick, taken in or not — a message
        # skipped for being another session's, or empty, must not be fetched
        # again for the rest of the session.
        for d in items:
            if isinstance(d, dict) and d.get("doc") == "inbox":
                try:
                    self._inbox_watermark = max(self._inbox_watermark, int(d.get("seq") or 0))
                except (TypeError, ValueError):
                    pass
        for doc in items:
            if not isinstance(doc, dict):
                continue
            # The label is stamped by the server from the passcode, not
            # taken from the page, so this is the one check that cannot be
            # spoofed from page code.
            if doc.get("reviewer") != OWNER or doc.get("sid") != self.sid:
                continue
            kind = doc.get("doc")
            if kind == "inbox" and isinstance(doc.get("msgs"), list):
                # The pre-2026-09-28 page: one numbered list per tab.
                for message in sorted(doc.get("msgs") or [], key=lambda m: m.get("seq", 0)):
                    seq = int(message.get("seq", 0))
                    if seq > self._inbox_seen and str(message.get("text", "")).strip():
                        self._inbox_seen = seq
                        await self._take_web_message(str(message["text"]))
                        acted += 1
            elif kind == "approvals":
                for rid, decision in (doc.get("decisions") or {}).items():
                    pending = self._approvals.get(rid)
                    if pending and not pending.future.done() and decision in {"allow", "always", "deny"}:
                        pending.future.set_result(decision)
                        acted += 1
            elif kind == "questions":
                for rid, answered in (doc.get("answers") or {}).items():
                    pending = self._questions_pending.get(rid)
                    if not (pending and not pending.future.done()):
                        continue
                    # The page sends {question_index: label_or_text}, or `None`
                    # on skip. Indices, because these keys become a Firestore
                    # property path and a path is capped at 1500 bytes — keyed
                    # by question text, a long question made every click fail
                    # to send. The tool wants them keyed by text, and the
                    # questions are on the event we posted, so map them here.
                    if answered in (None, {}):
                        pending.future.set_result(None)
                        acted += 1
                    elif isinstance(answered, dict):
                        pending.future.set_result(
                            self._name_answers(pending, answered))
                        acted += 1
            elif kind == "takeover":
                by = str(doc.get("by") or "")
                if by and by != self.sid and not self.taken_by:
                    self.taken_by = by
                    acted += 1
                    # Not awaited: this runs inside one of the tasks that
                    # yielding cancels.
                    asyncio.get_running_loop().create_task(self._yield_page(by))
            elif kind == "control":
                seq = int(doc.get("interrupt") or 0)
                if seq > self._interrupt_seen:
                    self._interrupt_seen = seq
                    self.interrupt_requested.set()
                    acted += 1
                mark = doc.get("viewer")
                if mark is not None and mark != self._viewer_mark:
                    self._viewer_mark = mark
                    self._viewer_at = time.monotonic()
                    # Someone arrived — a person at the page, or another
                    # session asking whether this one is still here. Beat now,
                    # and renew the lease, which is what answers the second.
                    self._lease_due = True
                    self._wake.set()
        return acted

    def _next_poll(self) -> float:
        """The interval to wait now, from what this session is doing."""
        now = time.monotonic()
        pending = (any(not a.future.done() for a in self._approvals.values())
                   or any(not q.future.done() for q in self._questions_pending.values()))
        viewer = self._viewer_at > 0 and now - self._viewer_at < VIEWER_WINDOW
        if self.listening:
            # The page is told nothing waits (0): a message is delivered as it
            # is written. The READ still happens, slowly, as the check.
            self._poll_s = 0.0
            return RECONCILE_EVERY
        # Kept for the head, which the page reads on the next heartbeat. Not
        # written now: it is a hint, and a write per change of cadence would
        # be traffic spent announcing that traffic had stopped.
        self._poll_s = poll_interval(now - self._last_activity,
                                     pending=pending, viewer=viewer)
        return self._poll_s

    def _listener_lost(self, why: str) -> None:
        """Stop believing the listener NOW, and wake the reader that has been
        sleeping ten minutes at a time on its word."""
        was = self.listening
        self._listen_at = 0.0
        self._listen_why = why
        self._poll_wake.set()
        if was:
            self.errors.append(f"listener: {why} — reading the inbox on a timer meanwhile")

    async def _listener(self) -> None:
        """Be told when the page writes. Ends only if this MCP cannot do it.

        Runs beside `_poller`, never instead of it: the reader is what the
        session falls back to the moment this stops answering, and what
        checks this while it does.
        """
        failures = 0
        while True:
            try:
                # Not through `_call`: that holds the lock every other call
                # here waits on, and this one is MEANT to stay open. The
                # server answers other calls while it waits.
                outcome = await asyncio.wait_for(
                    self._client.call("wait_for_responses", pub_id=self.link.pub_id,
                                      cursor=self._listen_cursor, timeout_s=LISTEN_WAIT),
                    timeout=LISTEN_WAIT + 20.0)
            except asyncio.TimeoutError:
                outcome = None
                why = f"no answer in {LISTEN_WAIT + 20:.0f}s"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                outcome = None
                why = f"{type(exc).__name__}: {exc}"
            else:
                why = "" if outcome.ok else str(outcome.error)
            if outcome is not None and not outcome.ok and _is_unknown_tool(outcome.error):
                # An MCP from before 2026-11-10. Not an error to retry: said
                # once, where a person will see it, and the timer carries on.
                self._listen_supported = False
                self._listen_why = ("this Scivo MCP has no wait_for_responses "
                                    "(before 2026-11-10) — `scivo update` adds it")
                self._poll_wake.set()
                print(f"\n  [scivo-control] {self._listen_why}.\n"
                      "  Until then the page's messages are read on a timer, which "
                      "is slower after a quiet spell and is billed per read.\n",
                      flush=True)
                return
            reply = outcome.first if outcome is not None and outcome.ok else None
            if not isinstance(reply, dict) or not reply.get("listening"):
                # `listening: false` is the server saying its stream is not
                # delivering: an empty answer from it means nothing. Asking
                # again makes a new stream; until one answers, the timer reads.
                self._listener_lost(why or str((reply or {}).get("why") or "not listening"))
                failures += 1
                await asyncio.sleep(min(60.0, 1.0 * 2 ** min(failures, 6)))
                continue
            failures = 0
            self._listen_cursor = str(reply.get("cursor") or "")
            regained = not self.listening and self._listen_trusted
            self._listen_at = time.monotonic()
            self._listen_why = ""
            if regained:
                self._poll_wake.set()       # the reader can go back to its slow check
            rows = reply.get("rows") or []
            if rows:
                await self._intake(rows)

    async def _wait_for_next_poll(self, last: float) -> None:
        """Sleep until the next read is due — re-deciding if this side wakes.

        The interval is decided when the sleep starts, and a long one would
        then outlast the idleness it was chosen for. `_touch` sets
        `_poll_wake`; each time it does, the due time is worked out again
        from what the session is doing NOW, against when it last read.
        """
        while True:
            remaining = last + self._next_poll() - time.monotonic()
            if remaining <= 0:
                return
            self._poll_wake.clear()
            try:
                await asyncio.wait_for(self._poll_wake.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return

    async def _poller(self) -> None:
        failures = 0
        last = time.monotonic()
        while True:
            await self._wait_for_next_poll(last)
            last = time.monotonic()
            # Guarded like _writer: one transport error here used to end the
            # task, and nothing awaited it — the heartbeat kept saying "live"
            # while messages, approvals and Stop were ignored for the rest of
            # the session and a pending approval deadlocked every later prompt.
            try:
                if self._since_seq_supported:
                    outcome = await self._call(
                        "list_responses", pub_id=self.link.pub_id,
                        since_seq=max(0, self._inbox_watermark - SEQ_SKEW_MS))
                    if not outcome.ok and _rejects_since_seq(outcome.error):
                        # An older MCP: fall back for the rest of the session
                        # rather than let the inbox go silent. Polling is then
                        # as expensive as it was before, and still correct.
                        self._since_seq_supported = False
                        self.errors.append(
                            "list_responses has no since_seq (MCP predates 2026-10-10) — "
                            "polling falls back to reading the whole inbox")
                        outcome = await self._call("list_responses", pub_id=self.link.pub_id)
                else:
                    outcome = await self._call("list_responses", pub_id=self.link.pub_id)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                self.errors.append(f"poll: {type(exc).__name__}: {exc}")
                if failures == 3:
                    self.status("the page cannot reach this session's inbox — messages from "
                                "the web are not arriving", level="error")
                # Doubling, capped: a network that is away stays away for
                # minutes, and eleven of these loops at a fixed half-second
                # was traffic spent on nothing during the one event where the
                # link is already in trouble.
                await asyncio.sleep(min(30.0, 0.5 * 2 ** failures))
                continue
            if not outcome.ok:
                # Counted and backed off, exactly like an exception. A refused
                # outcome used to fall through to `continue` with neither, and
                # bounding `_call` at 45s made that the COMMON path during an
                # outage: every session kept polling at full cadence and
                # nothing ever said the inbox was unreachable.
                failures += 1
                self.errors.append(f"poll: {outcome.error}")
                if failures == 3:
                    self.status("the page cannot reach this session's inbox — messages from "
                                "the web are not arriving", level="error")
                await asyncio.sleep(min(30.0, 0.5 * 2 ** failures))
                continue
            failures = 0
            told = self.listening
            acted = await self._intake(outcome.items)
            if told and acted:
                # The listener said it was delivering, and this read found
                # something it had not delivered. Once can be a write landing
                # in the same instant as the read; twice is a listener that
                # cannot be the only thing between the page and the session.
                self._listen_misses += 1
                self.errors.append(
                    f"listener: the reconcile read found {acted} thing(s) it had not "
                    f"delivered (time {self._listen_misses})")
                if self._listen_misses > LISTEN_MISSES_ALLOWED:
                    self._listen_trusted = False
                    self._listen_why = ("the listener missed changes the timer found — "
                                        "not relied on for the rest of this session")
                    self._poll_wake.set()
                    print(f"\n  [scivo-control] {self._listen_why}. "
                          "Messages still arrive; they are read on a timer.\n", flush=True)
