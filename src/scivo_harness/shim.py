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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MARKER = "[operator note] "
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "content-length"}


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


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
            message["content"] = "\n\n".join(pending + [_as_text(message.get("content"))])
            pending = []
        kept.append(message)

    if pending:
        # Nothing followed them. Attach to the last user turn, else hoist.
        for index in range(len(kept) - 1, -1, -1):
            if isinstance(kept[index], dict) and kept[index].get("role") == "user":
                kept[index] = dict(kept[index])
                kept[index]["content"] = "\n\n".join(
                    [_as_text(kept[index].get("content"))] + pending)
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

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
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
            while chunk := response.read(8192):
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")


def serve(upstream: str, port: int, verbose: bool = False) -> None:
    _Handler.upstream = upstream
    _Handler.verbose = verbose
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"scivo shim · 127.0.0.1:{port} → {upstream}")
    print("  folding mid-conversation system messages into the adjacent user turn")
    print("  point a provider's base_url at this address. ctrl-c to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
