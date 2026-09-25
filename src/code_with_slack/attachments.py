"""Files the owner attaches to a message: checked, downloaded, and handed to Claude.

A message with files arrives as `subtype: file_share` with a `files` array (measured 2026-09-25,
although Slack's reference calls that subtype legacy). An image Claude can read goes into the
prompt as an image block, as an image pasted into the terminal does (Agent SDK "Streaming Input",
read 2026-09-25); any other file is saved to a private folder and its path joins the prompt, as a
file dropped into the terminal does. A file past a limit, or one that fails to download, stops
the whole message: a prompt without its attachment would mislead Claude.
"""

import asyncio
import base64
import contextlib
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from code_with_slack import texts

# Claude's vision limits (platform.claude.com, "Vision", read 2026-09-25): JPEG, PNG, GIF and
# WebP, 10 MB base64-encoded per image through the API, 8000x8000 px.
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
IMAGE_LIMIT = 7_500_000  # bytes, whose base64 form is 10,000,000 characters
IMAGE_SIDE = 8000
# Per message (the maintainer, 2026-09-25): images stay in the conversation and are sent again
# at every turn, and a request is capped at 32 MB (Vision, read 2026-09-25).
IMAGES_PER_MESSAGE = 5
IMAGES_LIMIT = 15 * 1024 * 1024
FILE_LIMIT = 100 * 1024 * 1024
# The files that are not images Claude receives (the maintainer, 2026-09-25: the most common
# ones, those its Read tool opens): any text/* type, which covers source code, plus these.
FILE_TYPES = frozenset(
    {
        "application/pdf",
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/javascript",
        "application/x-sh",
        "application/sql",
        "application/toml",
        "application/x-ipynb+json",
    }
)
# Saved files outlive a restart, since the resumed conversation names them (the maintainer,
# 2026-09-25).
KEEP_SECONDS = 3 * 24 * 3600
# A file's URL receives the bot token: only Slack's own file host may have it.
FILE_HOST = "files.slack.com"
ORIGIN = ("https", FILE_HOST)
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)


class DownloadFailed(Exception):
    """A file could not be downloaded; the message says why, for the owner."""


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}MB"


def refusal(file: dict[str, Any]) -> str | None:
    """Why `file` cannot reach Claude, as the end of texts.UPLOAD_FAILED; None when it can."""
    url = urlsplit(str(file.get("url_private_download") or ""))
    if file.get("file_access", "visible") != "visible" or (url.scheme, url.hostname) != ORIGIN:
        return texts.UPLOAD_NOT_SHARED
    mimetype = str(file.get("mimetype") or "")
    size = int(file.get("size") or 0)
    if mimetype.startswith("image/"):
        if mimetype not in IMAGE_TYPES:
            return texts.UPLOAD_IMAGE_TYPE.format(mimetype=mimetype)
        if size > IMAGE_LIMIT:
            return texts.UPLOAD_IMAGE_SIZE.format(
                size=_megabytes(size), limit=_megabytes(IMAGE_LIMIT)
            )
        width, height = file.get("original_w"), file.get("original_h")
        if int(width or 0) > IMAGE_SIDE or int(height or 0) > IMAGE_SIDE:
            return texts.UPLOAD_IMAGE_SIDE.format(width=width, height=height)
    elif not (mimetype.startswith("text/") or mimetype in FILE_TYPES):
        return texts.UPLOAD_FILE_TYPE.format(mimetype=mimetype or "unknown")
    elif size > FILE_LIMIT:
        return texts.UPLOAD_FILE_SIZE.format(size=_megabytes(size), limit=_megabytes(FILE_LIMIT))
    return None


def images_refusal(files: list[dict[str, Any]]) -> str | None:
    """Why the images of one message, together, cannot reach Claude; None when they can."""
    images = [file for file in files if is_image(file)]
    if len(images) > IMAGES_PER_MESSAGE:
        return texts.UPLOAD_TOO_MANY.format(count=len(images), limit=IMAGES_PER_MESSAGE)
    total = sum(int(file.get("size") or 0) for file in images)
    if total > IMAGES_LIMIT:
        return texts.UPLOAD_TOO_HEAVY.format(size=_megabytes(total), limit=_megabytes(IMAGES_LIMIT))
    return None


def is_image(file: dict[str, Any]) -> bool:
    return file.get("mimetype") in IMAGE_TYPES


def limit_for(file: dict[str, Any]) -> int:
    return IMAGE_LIMIT if is_image(file) else FILE_LIMIT


async def download(
    url: str, token: str, mimetype: str, limit: int, origin: tuple[str, str] = ORIGIN
) -> bytes:
    """The file at `url`, fetched with the bot token (Slack's file object reference, read
    2026-09-25); raises DownloadFailed past `limit` bytes or on any failure."""
    parts = urlsplit(url)
    if (parts.scheme, parts.hostname) != origin:
        # The token goes to Slack's file host alone, whoever calls this.
        raise DownloadFailed(f"not a {FILE_HOST} URL")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with (
            aiohttp.ClientSession(timeout=DOWNLOAD_TIMEOUT) as http,
            # A redirect could carry the token to another host: none is followed.
            http.get(url, headers=headers, allow_redirects=False) as response,
        ):
            if response.status == 302:
                # Measured 2026-09-25: Slack's answer when the token lacks the scope.
                raise DownloadFailed(
                    "HTTP 302: Slack redirects instead of sending the file when the app lacks the "
                    "`files:read` scope"
                )
            if response.status != 200:
                raise DownloadFailed(f"HTTP {response.status}")
            if response.content_type == "text/html" and mimetype != "text/html":
                raise DownloadFailed(
                    "Slack sent a web page instead of the file: the app may lack the "
                    "`files:read` scope"
                )
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data += chunk
                if len(data) > limit:
                    raise DownloadFailed(f"larger than {_megabytes(limit)}")
            return bytes(data)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise DownloadFailed(type(exc).__name__) from exc


def saved_name(file: dict[str, Any]) -> str:
    """The file's name in the uploads folder: its Slack id first, so names never collide, and
    only the last part of the name, so it cannot point elsewhere."""
    name = Path(str(file.get("name") or "")).name
    return f"{file['id']}-{name}" if name not in ("", ".", "..") else str(file["id"])


def uploads_dir() -> Path:
    return Path(tempfile.gettempdir()) / "code-with-slack"


def _private(folder: Path) -> bool:
    """The folder is a real directory of this user that no one else can open."""
    try:
        info = folder.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def prepare_uploads(folder: Path) -> None:
    """Remove the saved files older than KEEP_SECONDS, from a folder only the owner can read."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _private(folder):
        return  # never touched: `save` refuses to write there and says so
    cutoff = time.time() - KEEP_SECONDS
    for path in folder.iterdir():
        with contextlib.suppress(OSError):
            if path.lstat().st_mtime < cutoff:
                path.unlink()


def _write(folder: Path, name: str, data: bytes) -> Path:
    # macOS may clean the temporary folder while the daemon runs.
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _private(folder):
        raise DownloadFailed(f"the uploads folder {folder} is not private to this user")
    path = folder / name
    path.write_bytes(data)
    return path


async def save(folder: Path, file: dict[str, Any], data: bytes) -> Path:
    return await asyncio.to_thread(_write, folder, saved_name(file), data)


def prompt_for(
    text: str, images: list[tuple[str, bytes]], paths: list[Path]
) -> str | list[dict[str, Any]]:
    """The owner's text with the saved files' paths; with images, one message of content blocks."""
    if paths:
        listed = "\n".join(f"- {path}" for path in paths)
        text = f"{text}\n\nAttached files:\n{listed}" if text else f"Attached files:\n{listed}"
    if not images:
        return text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    for mimetype, data in images:
        source = {"type": "base64", "media_type": mimetype, "data": base64.b64encode(data).decode()}
        blocks.append({"type": "image", "source": source})
    return blocks
