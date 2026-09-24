"""A translating proxy for local servers whose chat template rejects
mid-conversation system messages.

Claude Code sends operator instructions as `{"role": "system"}` entries inside
`messages[]` rather than editing the top-level `system` field — that is an
Anthropic API feature, and it exists so the cached prefix survives. Most local
GGUF chat templates were written before it. Qwen3's raises outright:

    Jinja Exception: System message must be at the beginning.

The session then dies on turn one with a 500 that names a template line number,
which looks like a broken harness and is not.

This shim folds each such message into the adjacent user turn and forwards
everything else untouched, streaming included. It is a workaround, and it does
change meaning slightly — a turn-scoped operator note becomes part of a user
message. The cleaner fix is to start `llama-server` with a chat template that
tolerates system messages anywhere; use this when restarting that server is not
free.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

MARKER = "[operator note] "
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "content-length"}


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _with_notes(content: Any, notes: list[str], *, before: bool) -> Any:
    """`content` with the notes added, every original block kept.

    A user turn that answers tool calls is a list of `tool_result` blocks.
    Flattening it to text to prepend a note deleted those results, so the model
    never saw its tools' output and called them again, forever. The note goes
    in as its own text block instead, after any tool results, because the
    Anthropic format wants those first in the turn.
    """
    joined = "\n\n".join(notes)
    if isinstance(content, str):
        return "\n\n".join([joined, content] if before else [content, joined])
    blocks = [b for b in content] if isinstance(content, list) else []
    note = {"type": "text", "text": joined}
    if before and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
        return [note] + blocks
    return blocks + [note]


def fold_system_messages(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Move every mid-conversation system message into a neighbouring user turn.

    Preferred target is the FOLLOWING user message, so the instruction still
    lands before the turn it was meant to govern. Falling back to the preceding
    user message keeps strict user/assistant alternation, which many templates
    also require — emitting two user messages in a row would trade one template
    error for another.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body, 0

    system_at = [i for i, m in enumerate(messages)
                 if isinstance(m, dict) and m.get("role") == "system"]
    if not system_at:
        return body, 0

    kept: list[Any] = []
    pending: list[str] = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            text = _as_text(message.get("content"))
            if text.strip():
                pending.append(MARKER + text)
            continue
        if pending and isinstance(message, dict) and message.get("role") == "user":
            message = dict(message)
            message["content"] = _with_notes(message.get("content"), pending, before=True)
            pending = []
        kept.append(message)

    if pending:
        # Nothing followed them. Attach to the last user turn, else hoist.
        for index in range(len(kept) - 1, -1, -1):
            if isinstance(kept[index], dict) and kept[index].get("role") == "user":
                kept[index] = dict(kept[index])
                kept[index]["content"] = _with_notes(kept[index].get("content"), pending, before=False)
                pending = []
                break
    if pending:
        body["system"] = "\n\n".join([_as_text(body.get("system", ""))] + pending).strip()

    body["messages"] = kept
    return body, len(system_at)


class _Handler(BaseHTTPRequestHandler):
    upstream = "http://localhost:8190"
    verbose = False
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.verbose:
            super().log_message(fmt, *args)

    def _read_body(self) -> bytes:
        """The request body, chunked or with a Content-Length. A chunked POST
        used to read as empty, and fold_system_messages then folded nothing."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = bytearray()
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    break
                size = int(size_line.split(b";", 1)[0], 16)
                if size == 0:
                    self.rfile.readline()   # trailing CRLF
                    break
                out += self.rfile.read(size)
                self.rfile.readline()       # CRLF after the chunk
            return bytes(out)
        return self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))

    def do_POST(self) -> None:  # noqa: N802
        raw = self._read_body()
        folded = 0
        # The CLI calls /v1/messages?beta=true — match the route, not the URL.
        route = self.path.split("?", 1)[0].rstrip("/")
        if route.endswith("/v1/messages"):
            try:
                body, folded = fold_system_messages(json.loads(raw))
                raw = json.dumps(body).encode()
            except (json.JSONDecodeError, AttributeError):
                pass  # not JSON we understand; forward as-is
        if folded and self.verbose:
            print(f"  folded {folded} mid-conversation system message(s)")

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        request = urllib.request.Request(
            self.upstream.rstrip("/") + self.path, data=raw, headers=headers, method="POST")
        self._relay(request)

    def do_GET(self) -> None:  # noqa: N802
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        self._relay(urllib.request.Request(
            self.upstream.rstrip("/") + self.path, headers=headers, method="GET"))

    def _relay(self, request: urllib.request.Request) -> None:
        try:
            response = urllib.request.urlopen(request, timeout=900)
        except urllib.error.HTTPError as exc:
            response = exc
        except Exception as exc:  # noqa: BLE001
            self.send_response(502)
            self.send_header("content-type", "application/json")
            payload = json.dumps({"type": "error", "error": {
                "type": "api_error", "message": f"shim could not reach upstream: {exc}"}}).encode()
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        with response:
            self.send_response(response.status)
            for key, value in response.headers.items():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            # read1: whatever has arrived. read(8192) blocked until 8 KB had
            # accumulated, so a short SSE reply appeared only when the turn
            # ended.
            reader = getattr(response, "read1", response.read)
            while chunk := reader(8192):
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")


class _QuietServer(ThreadingHTTPServer):
    """A dropped connection is normal here, not an incident.

    Claude Code opens and abandons connections as turns end, and the stdlib
    server prints a full traceback for each one. In a session that traceback
    lands on top of the prompt and reads like a crash. Anything else still
    prints — a real bug in the shim must not be swallowed with them.
    """

    QUIET = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], self.QUIET):
            return
        super().handle_error(request, client_address)


def start_background(upstream: str) -> tuple[str, "Callable[[], None]"]:
    """Run the shim inside this process on a free port; return its URL and a stop.

    Sessions start this themselves when the local server needs it, so nobody
    has to know the shim exists. A per-call handler subclass keeps two shims in
    one process from sharing an upstream.
    """
    import threading

    handler = type("BoundShimHandler", (_Handler,), {"upstream": upstream, "verbose": False})
    server = _QuietServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, name="scivo-shim", daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return f"http://127.0.0.1:{server.server_address[1]}", stop


def serve(upstream: str, port: int, verbose: bool = False) -> None:
    _Handler.upstream = upstream
    _Handler.verbose = verbose
    server = _QuietServer(("127.0.0.1", port), _Handler)
    print(f"scivo shim · 127.0.0.1:{port} → {upstream}")
    print("  folding mid-conversation system messages into the adjacent user turn")
    print("  point a provider's base_url at this address. ctrl-c to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
