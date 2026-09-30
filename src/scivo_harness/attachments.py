"""Images the person sends IN — from the web page or the terminal.

The model reads images as content blocks in a user message, and until now
nothing built one: a screenshot pasted into the control page was dropped by
the browser, and a terminal has no image paste at all (2026-09-30). Two
routes end here:

- web: the page reads a pasted or dropped image, downscales it in the
  browser, and writes it as a data URL beside the message text;
- terminal: `/paste` reads the system clipboard through the platform's
  clipboard tool, `/img <path>` attaches a file, and a line that is only the
  path of an image file attaches it (dropping a file onto most terminals
  pastes its path).

Either way the bytes are saved under the project's `.scivo/inbox/` (already
ignored by git) so the model can `Read` them again later, and the next
message carries them as base64 blocks so it sees them without a tool call.
"""
from __future__ import annotations

import base64
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

IMAGE_SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".gif": "image/gif", ".webp": "image/webp"}
MAX_BLOCK_BYTES = 3_000_000       # the API's per-image ceiling is ~5 MB; stay well under
_DATA_URL = re.compile(r"^data:(image/[a-z+]+);base64,(.+)$", re.S)


def decode_data_url(url: str) -> tuple[bytes, str] | None:
    m = _DATA_URL.match(url.strip())
    if not m:
        return None
    try:
        return base64.b64decode(m.group(2)), m.group(1)
    except Exception:  # noqa: BLE001
        return None


def read_clipboard_image() -> tuple[bytes, str] | None:
    """The image on the system clipboard, via whichever tool this platform has.

    Wayland: wl-paste. X11: xclip. macOS: pngpaste (brew install pngpaste).
    Windows: PowerShell. None found, or no image on the clipboard → None."""
    attempts: list[list[str]] = []
    if sys.platform == "darwin":
        attempts.append(["pngpaste", "-"])
    elif sys.platform.startswith("win"):
        attempts.append(["powershell", "-NoProfile", "-Command",
                         "$i=Get-Clipboard -Format Image; if($i){$m=New-Object IO.MemoryStream;"
                         "$i.Save($m,[Drawing.Imaging.ImageFormat]::Png);[Console]::OpenStandardOutput()"
                         ".Write($m.ToArray(),0,$m.Length)}"])
    else:
        attempts.append(["wl-paste", "-t", "image/png"])
        attempts.append(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
    for cmd in attempts:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            continue
        if out.returncode == 0 and out.stdout[:8] == b"\x89PNG\r\n\x1a\n":
            return out.stdout, "image/png"
    return None


def clipboard_tool_hint() -> str:
    if sys.platform == "darwin":
        return "needs pngpaste: brew install pngpaste"
    if sys.platform.startswith("win"):
        return "needs PowerShell on PATH"
    return "needs wl-paste (Wayland) or xclip (X11): sudo apt install -y wl-clipboard xclip"


def image_path_in(line: str) -> Path | None:
    """A line that is nothing but the path of an existing image file."""
    text = line.strip().strip("'\"")
    if not text or "\n" in text or " " in text and not Path(text).exists():
        return None
    p = Path(text).expanduser()
    if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file():
        return p.resolve()
    return None


def save_inbound(root: Path, data: bytes, mime: str, stem: str = "") -> Path:
    """Write the bytes under <root>/.scivo/inbox and return the path."""
    ext = {v: k for k, v in IMAGE_SUFFIXES.items()}.get(mime, ".png")
    folder = root / ".scivo" / "inbox"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{stem or 'img'}{ext}"
    path = folder / name
    path.write_bytes(data)
    return path


def shrink(data: bytes, mime: str) -> tuple[bytes, str]:
    """Downscale for the model, when Pillow is here; otherwise as-is."""
    from .control import _shrink_for_web
    suffix = {v: k for k, v in IMAGE_SUFFIXES.items()}.get(mime, ".png")
    out, new_mime, _ = _shrink_for_web(data, mime, suffix)
    return out, new_mime


def user_message(text: str, images: list[Path]) -> dict[str, Any]:
    """The SDK user message carrying `text` and the images as base64 blocks.

    The paths are named in the text too, so the model can Read one again
    after the conversation is compacted and the blocks are gone."""
    blocks: list[dict[str, Any]] = []
    kept: list[Path] = []
    for p in images:
        try:
            data = p.read_bytes()
        except OSError:
            continue
        mime = IMAGE_SUFFIXES.get(p.suffix.lower(), "image/png")
        data, mime = shrink(data, mime)
        if len(data) > MAX_BLOCK_BYTES:
            continue
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                    "data": base64.b64encode(data).decode("ascii")}})
        kept.append(p)
    note = ""
    if kept:
        note = "\n\n[attached image" + ("s" if len(kept) > 1 else "") + ": " + ", ".join(str(p) for p in kept) + "]"
    blocks.append({"type": "text", "text": (text or "(see the attached image)") + note})
    return {"type": "user", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}


async def one(message: dict[str, Any]):
    """An async iterable of exactly one message, which is what query() takes
    for anything that is not a plain string."""
    yield message
