import base64
import copy
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from code_with_slack import texts
from code_with_slack.attachments import (
    FILE_LIMIT,
    IMAGE_LIMIT,
    IMAGES_LIMIT,
    IMAGES_PER_MESSAGE,
    KEEP_SECONDS,
    DownloadFailed,
    download,
    images_refusal,
    prepare_uploads,
    prompt_for,
    refusal,
    save,
    saved_name,
)
from tests.fakes import slack_payload


def shared(kind: str, **fields: Any) -> dict[str, Any]:
    """The file object of a recorded file_share message (scrubbed, Slack 2026-09-25)."""
    name = {"image": "100", "screenshot": "101", "snippet": "102"}[kind]
    body = slack_payload(f"{name}-event_callback-file_share-{kind}")
    return {**copy.deepcopy(body["event"]["files"][0]), **fields}


def test_recorded_images_and_snippets_are_accepted() -> None:
    assert [refusal(shared(k)) for k in ("image", "screenshot", "snippet")] == [None] * 3


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"mimetype": "image/heic"}, texts.UPLOAD_IMAGE_TYPE.format(mimetype="image/heic")),
        ({"size": IMAGE_LIMIT + 1}, texts.UPLOAD_IMAGE_SIZE.format(size="7.2MB", limit="7.2MB")),
        ({"original_w": 8001}, texts.UPLOAD_IMAGE_SIDE.format(width=8001, height=None)),
        ({"file_access": "check_file_info"}, texts.UPLOAD_NOT_SHARED),
        ({"url_private_download": "https://evil.example/x.png"}, texts.UPLOAD_NOT_SHARED),
        ({"url_private_download": "http://files.slack.com/x.png"}, texts.UPLOAD_NOT_SHARED),
    ],
)
def test_an_image_past_claude_s_limits_is_refused_with_the_reason(
    fields: dict[str, Any], reason: str
) -> None:
    file = shared("image", **fields)
    if "original_w" in fields:
        reason = texts.UPLOAD_IMAGE_SIDE.format(width=8001, height=file["original_h"])
    assert refusal(file) == reason


def test_a_file_past_the_limit_is_refused() -> None:
    assert refusal(shared("snippet", size=FILE_LIMIT + 1)) == texts.UPLOAD_FILE_SIZE.format(
        size="100.0MB", limit="100.0MB"
    )


def test_a_saved_name_cannot_leave_the_uploads_folder() -> None:
    assert saved_name({"id": "F000FILE", "name": "../../etc/passwd"}) == "F000FILE-passwd"
    assert saved_name({"id": "F000FILE", "name": ""}) == "F000FILE"


def test_text_and_files_make_a_plain_prompt() -> None:
    prompt = prompt_for("Summarize this file.", [], [Path("/tmp/cws/F1-notes.txt")])
    assert prompt == "Summarize this file.\n\nAttached files:\n- /tmp/cws/F1-notes.txt"


def test_images_make_one_message_of_content_blocks() -> None:
    blocks = prompt_for("What is in this image?", [("image/png", b"\x89PNG")], [])
    assert blocks == [
        {"type": "text", "text": "What is in this image?"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(b"\x89PNG").decode(),
            },
        },
    ]
    assert prompt_for("", [("image/png", b"x")], [])[0]["type"] == "image"  # type: ignore[index]


LOCAL = ("http", "127.0.0.1")  # the test server; the daemon's origin is https://files.slack.com


async def serve(tmp_path: Path, handler: Any) -> tuple[str, web.AppRunner]:
    app = web.Application()
    app.router.add_get("/file", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return f"http://127.0.0.1:{port}/file", runner


async def test_download_sends_the_bot_token_and_returns_the_bytes(tmp_path: Path) -> None:
    seen: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(request.headers.get("Authorization", ""))
        return web.Response(body=b"hello\n", content_type="text/plain")

    url, runner = await serve(tmp_path, handler)
    try:
        got = await download(url, "xox" + "b-fake", "text/plain", limit=100, origin=LOCAL)
        assert got == b"hello\n"
    finally:
        await runner.cleanup()
    assert seen == ["Bearer " + "xox" + "b-fake"]


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (web.Response(status=404), "HTTP 404"),
        # Measured 2026-09-25: without files:read Slack answers 302; with it, 200 and the file.
        (
            web.Response(status=302, headers={"Location": "https://elsewhere.example/"}),
            "files:read",
        ),
        (web.Response(body=b"x" * 101, content_type="text/plain"), "larger than"),
        # Without files:read, Slack answers with its sign-in page instead of the file.
        (web.Response(body=b"<html>", content_type="text/html"), "files:read"),
    ],
)
async def test_a_failed_download_says_why(
    tmp_path: Path, response: web.Response, error: str
) -> None:
    async def handler(request: web.Request) -> web.Response:
        return response

    url, runner = await serve(tmp_path, handler)
    try:
        with pytest.raises(DownloadFailed, match=error):
            await download(url, "xox" + "b-fake", "image/png", limit=100, origin=LOCAL)
    finally:
        await runner.cleanup()


async def test_the_token_never_leaves_for_another_host() -> None:
    # Checked inside download too, beside the header it protects: not only in refusal().
    with pytest.raises(DownloadFailed, match=r"files\.slack\.com"):
        await download("http://127.0.0.1:9/file", "xox" + "b-fake", "text/plain", limit=100)


def test_a_message_takes_at_most_five_images_and_fifteen_megabytes() -> None:
    # the maintainer, 2026-09-25: images stay in the history and are sent again at every turn.
    image = shared("image", size=1_000_000)
    assert images_refusal([image] * IMAGES_PER_MESSAGE) is None
    assert images_refusal([image] * (IMAGES_PER_MESSAGE + 1)) == texts.UPLOAD_TOO_MANY.format(
        count=IMAGES_PER_MESSAGE + 1, limit=IMAGES_PER_MESSAGE
    )
    big = shared("image", size=IMAGE_LIMIT)
    assert images_refusal([big, big, big]) == texts.UPLOAD_TOO_HEAVY.format(
        size="21.5MB", limit=f"{IMAGES_LIMIT / 1024 / 1024:.1f}MB"
    )
    assert images_refusal([shared("snippet", size=FILE_LIMIT)] * 9) is None  # files are paths


async def test_saved_files_last_three_days(tmp_path: Path) -> None:
    folder = tmp_path / "uploads"
    old = await save(folder, {"id": "F1", "name": "old.txt"}, b"x")
    fresh = await save(folder, {"id": "F2", "name": "fresh.txt"}, b"x")
    stamp = old.stat().st_mtime - KEEP_SECONDS - 60
    import os

    os.utime(old, (stamp, stamp))
    prepare_uploads(folder)
    assert not old.exists() and fresh.exists()  # a resumed session still finds its files
    assert KEEP_SECONDS == 3 * 24 * 3600  # the maintainer, 2026-09-25


async def test_a_folder_that_is_not_private_is_never_written(tmp_path: Path) -> None:
    folder = tmp_path / "uploads"
    folder.mkdir(mode=0o777)
    folder.chmod(0o777)
    with pytest.raises(DownloadFailed, match="not private"):
        await save(folder, {"id": "F1", "name": "a.txt"}, b"x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(elsewhere)
    with pytest.raises(DownloadFailed, match="not private"):
        await save(link, {"id": "F1", "name": "a.txt"}, b"x")


@pytest.mark.parametrize(
    "mimetype",
    [
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/x-python",
        "application/pdf",
        "application/json",
        "application/x-yaml",
        "application/xml",
        "application/x-ipynb+json",
    ],
)
def test_common_readable_files_pass(mimetype: str) -> None:
    assert refusal(shared("snippet", mimetype=mimetype)) is None


@pytest.mark.parametrize(
    "mimetype",
    [
        "application/zip",
        "application/octet-stream",
        "application/x-msdownload",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "",
    ],
)
def test_other_files_are_refused_with_their_type(mimetype: str) -> None:
    # the maintainer, 2026-09-25: the most common files only, those Claude reads.
    shown = mimetype or "unknown"
    assert refusal(shared("snippet", mimetype=mimetype)) == texts.UPLOAD_FILE_TYPE.format(
        mimetype=shown
    )


async def test_a_file_that_cannot_be_written_says_why(tmp_path: Path) -> None:
    folder = tmp_path / "uploads"
    folder.mkdir(mode=0o500)  # private, but not writable
    try:
        with pytest.raises(DownloadFailed, match="could not be saved"):
            await save(folder, {"id": "F1", "name": "a.txt"}, b"x")
    finally:
        folder.chmod(0o700)


async def test_a_network_failure_keeps_its_detail() -> None:
    # Port 9 on localhost refuses the connection: the owner sees why, not a class name alone.
    with pytest.raises(DownloadFailed, match=r"ClientConnectorError: .+"):
        await download("http://127.0.0.1:9/file", "xox" + "b-fake", "text/plain", 100, origin=LOCAL)


def test_a_folder_that_is_not_private_is_logged_at_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = tmp_path / "uploads"
    folder.mkdir(mode=0o777)
    folder.chmod(0o777)
    with caplog.at_level("WARNING"):
        prepare_uploads(folder)
    assert "not private" in caplog.text
