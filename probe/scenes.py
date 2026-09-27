"""The scenes: each one causes the events a claim needs, through the daemon's own SessionManager.

Claude Code is real (the SDK's bundled CLI, the owner's login, Haiku); Slack is the test suite's
FakeSlack, since an SDK release changes nothing on the Slack side. What a claim checks is what the
owner would see: the text of the replies, the running line, the answer of `!status`.
"""

import asyncio
import contextlib
import dataclasses
import json
import secrets
import shutil
import struct
import tempfile
import zlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from code_with_slack.approvals import Approvals, Approve, Deny, Pending
from code_with_slack.attachments import prompt_for
from code_with_slack.footer import UsageCache
from code_with_slack.guards import Identity
from code_with_slack.resume import resume_blocks
from code_with_slack.sessions import (
    ChannelSession,
    ClaudeClient,
    SessionDeps,
    SessionManager,
    Turn,
    directory_sessions,
)
from code_with_slack.state import StateStore
from probe.claims import Observation
from tests.fakes import FakeSlack

# The model the fixture recorder uses: the cheapest that runs every scene.
PROBE_MODEL = "claude-haiku-4-5-20251001"
CHANNEL, OWNER, TEAM, BOT = "C000PROBE", "U000PROBE", "T000PROBE", "U000PROBEBOT"
TURN_LIMIT = 180.0
Log = Callable[[str], None]


class ProbeApprovals(Approvals):
    """Answers every request as the owner would: Approve a tool, Deny a question."""

    def __init__(self) -> None:
        super().__init__()
        self.titles: list[str] = []

    def open(
        self, channel_id: str, title: str, questions: list[dict[str, Any]] | None = None
    ) -> tuple[str, Pending]:
        approval_id, pending = super().open(channel_id, title, questions)
        self.titles.append(title)
        decision = Deny() if questions else Approve()
        asyncio.get_running_loop().call_soon(self.resolve, approval_id, channel_id, decision)
        return approval_id, pending


def probe_client(options: ClaudeAgentOptions) -> ClaudeClient:
    return ClaudeSDKClient(dataclasses.replace(options, model=PROBE_MODEL))


def one_pixel_png() -> bytes:
    """A valid 1x1 red PNG, built here so the probe ships no binary file."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


class Stage:
    def __init__(self, workdir: Path, log: Log) -> None:
        self.workdir = workdir
        self.log = log
        self.slack = FakeSlack()
        self.approvals = ProbeApprovals()
        self.state = StateStore(workdir.parent / "state.json")
        self.state.bind(CHANNEL, workdir)

        async def trusted(directory: Path) -> bool:
            return directory == workdir

        async def no_usage() -> str:
            return ""

        self.manager = SessionManager(
            SessionDeps(
                slack=self.slack,
                identity=Identity(OWNER, TEAM, BOT),
                state=self.state,
                approvals=self.approvals,
                usage=UsageCache(no_usage),
                client_factory=probe_client,
                workspace_trusted=trusted,
            )
        )

    @property
    def session(self) -> ChannelSession:
        session = self.manager.get(CHANNEL)
        assert session is not None
        return session

    def last_reply(self) -> str:
        texts = self.slack.message_texts()
        return texts[-1] if texts else ""

    def shown_now(self) -> str:
        """What the channel shows now: each message's last blocks, footer and running line
        included, deleted messages left out."""
        posted = iter(self.slack.posted_ts)
        shown: dict[str, str] = {}
        for method, args in self.slack.calls:
            if method == "chat.postMessage":
                shown[next(posted)] = json.dumps(args.get("blocks") or [], ensure_ascii=False)
            elif method == "chat.update":
                shown[args["ts"]] = json.dumps(args.get("blocks") or [], ensure_ascii=False)
            elif method == "chat.delete":
                shown.pop(args["ts"], None)
        return "\n".join(shown.values())

    def tool_lines_ever(self, since: int = 0) -> str:
        """Every tool line a reply showed after call `since`, even one folded into the counts
        when its turn ended."""
        return "\n".join(
            json.dumps(b, ensure_ascii=False)
            for method, args in self.slack.calls[since:]
            if method in ("chat.postMessage", "chat.update")
            for b in args.get("blocks") or []
            if str(b.get("block_id", "")).startswith("tools-")
        )

    async def turn(self, prompt: Any) -> Turn:
        turn = await self.session.submit(prompt)
        await asyncio.wait_for(turn.done.wait(), TURN_LIMIT)
        return turn


async def until(condition: Callable[[], bool], limit: float) -> bool:
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(limit):
            while not condition():  # noqa: ASYNC110
                await asyncio.sleep(0.2)
    return condition()


async def attempt(
    name: str, log: Log, scene: Awaitable[dict[str, Observation]]
) -> dict[str, Observation]:
    """A scene that raises proves nothing about the claims it did not reach."""
    log(f"scene: {name}")
    try:
        return await scene
    except Exception as exc:
        log(f"scene {name} stopped: {type(exc).__name__}: {exc}")
        return {}


async def first_turn(s: Stage, word: str) -> dict[str, Observation]:
    await s.turn(f"Remember this code word: {word}. Reply with the single word: ready")
    stored = s.state.get(CHANNEL)
    status = await s.session.status()
    return {
        "P1": Observation(True, s.session.cli_version is not None, f"cli {s.session.cli_version}"),
        "P3": Observation(True, bool(stored and stored.session_id)),
        "P2": Observation(True, "Context:" in status),
    }


async def image_turn(s: Stage) -> dict[str, Observation]:
    before = len(s.slack.posted_ts)
    await s.turn(
        prompt_for(
            "Reply with one word: what colour is this image?", [("image/png", one_pixel_png())], []
        )
    )
    reply = s.last_reply()
    return {
        "P4": Observation(
            len(s.slack.posted_ts) > before, bool(reply) and "reported an error" not in reply
        )
    }


async def file_turn(s: Stage, folder: Path) -> dict[str, Observation]:
    token = secrets.token_hex(4)
    path = folder / "probe-note.txt"
    path.write_text(f"{token}\n")
    await s.turn(prompt_for("Read the attached file and reply with its content only.", [], [path]))
    return {"P5": Observation(True, token in s.last_reply())}


async def bash_turn(s: Stage) -> dict[str, Observation]:
    asked, calls = len(s.approvals.titles), len(s.slack.calls)
    marker = s.workdir / "probe-ran.txt"
    await s.turn(
        "Use the Bash tool to run exactly this command: echo ok > probe-ran.txt\nThen reply: done"
    )
    # Claude may write the file with another tool: only a Bash call counts, known by its approval
    # request. The file shows that it ran. The reply's tool line may show the command only while
    # it runs, depending on update timing; once folded it still names the tool (`✓ Bash`).
    bash_asked = any(t.startswith("Bash") for t in s.approvals.titles[asked:])
    shown = "Bash" in s.tool_lines_ever(since=calls)
    return {
        "P10": Observation(bash_asked, shown, "" if bash_asked else "no Bash approval asked"),
        "P11": Observation(
            bash_asked, marker.exists(), "" if bash_asked else "no Bash approval asked"
        ),
    }


async def background_stop(s: Stage) -> dict[str, Observation]:
    await s.turn(
        "Use the Bash tool with run_in_background set to true to run: tail -f /dev/null\n"
        "Do not wait for it. Reply: started"
    )
    running = await until(lambda: "1 shell" in s.shown_now(), 20)
    if not running:
        return {"P12": Observation(False, False, "no background command started")}
    stopped = await s.session.stop()
    gone = await until(lambda: "1 shell" not in s.shown_now(), 30)
    return {"P12": Observation(True, stopped and gone)}


async def interrupt(s: Stage) -> dict[str, Observation]:
    turn = await s.session.submit(
        "Write the numbers from 1 to 400, one per line, and nothing else."
    )
    writing = await until(lambda: s.session.busy, 60)
    await asyncio.sleep(2)
    stopped = await s.session.stop() if writing else False
    ended = await until(turn.done.is_set, 60)
    return {"P8": Observation(stopped, ended)}


async def resume(s: Stage, word: str) -> dict[str, Observation]:
    stored = s.state.get(CHANNEL)
    session_id = stored.session_id if stored else None
    listed = await asyncio.to_thread(directory_sessions, s.workdir)
    picker = json.dumps(resume_blocks(s.workdir, listed, None, datetime.now(UTC)))
    if session_id is None:
        return {"P6": Observation(False, False, "no session id stored")}
    in_list = any(i.session_id == session_id for i in listed) and session_id[:8] in picker
    seen = {"P6": Observation(True, in_list)}
    if not await s.manager.resume(CHANNEL, session_id):
        return seen
    await s.turn("What was the code word I gave you? Reply with the word only.")
    return seen | {"P7": Observation(True, word in s.last_reply())}


async def bypass(s: Stage) -> dict[str, Observation]:
    # Neither `!status` (the stored switch) nor the CLI's server info (unchanged by a mode set
    # on a live client, measured 2026-09-27 on 2.1.283) shows the mode: its only effect is a
    # command that runs without asking.
    await s.session.set_bypass(True)
    asked = len(s.approvals.titles)
    marker = s.workdir / "bypass-ran.txt"
    try:
        await s.turn(
            "Use the Bash tool to run exactly this command: echo ok > bypass-ran.txt\n"
            "Then reply: done"
        )
    finally:
        await s.session.set_bypass(False)
    ran = marker.exists()
    unasked = not any(t.startswith("Bash") for t in s.approvals.titles[asked:])
    return {"P9": Observation(ran, unasked, "" if ran else "no Bash call")}


def forget_sessions(workdir: Path) -> None:
    """Remove the transcripts the probe's sessions left under ~/.claude/projects: only the one
    folder named after this run's temporary directory, whose name is random."""
    projects = Path.home() / ".claude" / "projects"
    matches = [p for p in projects.glob(f"*{workdir.name}") if p.is_dir()]
    if len(matches) == 1:
        shutil.rmtree(matches[0])


def folders(root: str) -> tuple[Path, Path]:
    """The session's working directory, named at random so its transcripts can be found and
    removed, and a folder outside it for attachments, as the daemon's uploads folder is."""
    base = Path(root).resolve()
    workdir, uploads = base / f"cws-probe-{secrets.token_hex(4)}", base / "uploads"
    workdir.mkdir()
    uploads.mkdir()
    return workdir, uploads


async def run_scenes(log: Log) -> dict[str, Observation]:
    seen: dict[str, Observation] = {}
    with tempfile.TemporaryDirectory(prefix="cws-probe-") as root:
        workdir, uploads = folders(root)
        stage = Stage(workdir, log)
        word = secrets.token_hex(3)
        try:
            seen |= await attempt("first turn", log, first_turn(stage, word))
            seen |= await attempt("image", log, image_turn(stage))
            seen |= await attempt("file", log, file_turn(stage, uploads))
            seen |= await attempt("bash and approval", log, bash_turn(stage))
            seen |= await attempt("background and stop", log, background_stop(stage))
            seen |= await attempt("interrupt", log, interrupt(stage))
            seen |= await attempt("resume", log, resume(stage, word))
            seen |= await attempt("bypass", log, bypass(stage))
        finally:
            await stage.manager.close_all()
            forget_sessions(workdir)
    return seen
