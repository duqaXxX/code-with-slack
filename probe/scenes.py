"""The scenes: each one causes the events a claim needs, through the daemon's own SessionManager.

Claude Code is real (the SDK's bundled CLI, the owner's login, Haiku); Slack is the test suite's
FakeSlack, since an SDK release changes nothing on the Slack side. What a claim checks is what the
owner would see: the text of the replies, the running line, the approvals asked.
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
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResult,
    ToolPermissionContext,
)

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
COUNT_TO = 2000
# Failures of the machine the probe runs on, not of the SDK: a turn past TURN_LIMIT, the network.
# Such a scene learned nothing, so its claims are UNPROVEN rather than BROKEN.
ENVIRONMENT = (TimeoutError, ConnectionError, OSError)
Log = Callable[[str], None]
CanUseTool = Callable[[str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResult]]


class ProbeApprovals(Approvals):
    """Answers every request as the owner would: Approve a tool, Deny a question."""

    def open(
        self, channel_id: str, title: str, questions: list[dict[str, Any]] | None = None
    ) -> tuple[str, Pending]:
        approval_id, pending = super().open(channel_id, title, questions)
        decision = Deny() if questions else Approve()
        asyncio.get_running_loop().call_soon(self.resolve, approval_id, channel_id, decision)
        return approval_id, pending


def one_pixel_png() -> bytes:
    """A valid 1x1 red PNG, built here so the probe ships no binary file."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels)
    return png + chunk(b"IEND", b"")


class Mark(NamedTuple):
    """Where the channel stands before a scene: what came after it is the scene's."""

    posted: int  # messages posted
    calls: int  # Slack calls made
    asked: int  # permission requests received


class Stage:
    def __init__(self, workdir: Path, log: Log) -> None:
        self.workdir = workdir
        self.log = log
        self.slack = FakeSlack()
        # The tool of every permission request the CLI sent, as `can_use_tool` received it.
        self.asked: list[str] = []
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
                approvals=ProbeApprovals(),
                usage=UsageCache(no_usage),
                client_factory=self.client,
                workspace_trusted=trusted,
            )
        )

    def client(self, options: ClaudeAgentOptions) -> ClaudeClient:
        """The real client, on Haiku, noting which tool each permission request is for."""
        decide: CanUseTool | None = options.can_use_tool

        async def noted(
            tool: str, tool_input: dict[str, Any], context: ToolPermissionContext
        ) -> PermissionResult:
            self.asked.append(tool)
            assert decide is not None
            return await decide(tool, tool_input, context)

        can_use_tool = noted if decide is not None else None
        return ClaudeSDKClient(
            dataclasses.replace(options, model=PROBE_MODEL, can_use_tool=can_use_tool)
        )

    @property
    def session(self) -> ChannelSession:
        session = self.manager.get(CHANNEL)
        assert session is not None
        return session

    def mark(self) -> Mark:
        return Mark(len(self.slack.posted_ts), len(self.slack.calls), len(self.asked))

    def replies_since(self, mark: Mark) -> str:
        """The text of every message posted since `mark`: the turn's reply, wherever an approval
        request or a notice came in between."""
        return "\n".join(self.slack.message_texts()[mark.posted :])

    def tool_lines_since(self, mark: Mark) -> str:
        """Every tool line a reply showed since `mark`, even one folded when its turn ended."""
        return "\n".join(
            json.dumps(b, ensure_ascii=False)
            for method, args in self.slack.calls[mark.calls :]
            if method in ("chat.postMessage", "chat.update")
            for b in args.get("blocks") or []
            if str(b.get("block_id", "")).startswith("tools-")
        )

    def asked_since(self, mark: Mark) -> list[str]:
        return self.asked[mark.asked :]

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

    async def turn(self, prompt: Any) -> Turn:
        turn = await self.session.submit(prompt)
        await asyncio.wait_for(turn.done.wait(), TURN_LIMIT)
        return turn

    async def recover(self) -> None:
        """After a scene that raised: stop what it left running, so the next scene starts on an
        idle channel instead of queueing behind a turn that timed out."""
        with contextlib.suppress(Exception):
            await self.session.stop()
            await until(lambda: self.session.idle, 30)


async def until(condition: Callable[[], bool], limit: float) -> bool:
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(limit):
            while not condition():  # noqa: ASYNC110
                await asyncio.sleep(0.2)
    return condition()


async def attempt(
    name: str,
    s: Stage,
    scene: Coroutine[Any, Any, dict[str, Observation]],
) -> dict[str, Observation]:
    """Run a scene. One that raises did cause its events and saw them fail, since a break in the
    SDK most often shows as a call that raises; a failure of the machine instead (a timeout, the
    network) learned nothing. Its claims record which, with the exception."""
    claims = SCENES[name]
    s.log(f"scene: {name}")
    try:
        seen = await scene
    except Exception as exc:
        detail = f"scene raised {type(exc).__name__}: {exc}"
        s.log(f"scene {name}: {detail}")
        await s.recover()
        caused = not isinstance(exc, ENVIRONMENT)
        return {claim: Observation(caused, False, detail) for claim in claims}
    if unknown := set(seen) - set(claims):
        raise ValueError(f"scene {name} observed claims it does not own: {sorted(unknown)}")
    return seen


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
    mark = s.mark()
    image = [("image/png", one_pixel_png())]
    await s.turn(prompt_for("Reply with one word: what colour is this image?", image, []))
    reply = s.replies_since(mark)
    # The pixel is red: naming its colour shows Claude received the image. A reply alone does not,
    # since an error such as `API Error: 400` is a reply too.
    start = " ".join(reply.split())[:80]
    return {
        "P4": Observation(True, "red" in reply.lower(), "" if "red" in reply.lower() else start)
    }


async def file_turn(s: Stage, folder: Path) -> dict[str, Observation]:
    mark, token = s.mark(), secrets.token_hex(4)
    path = folder / "probe-note.txt"
    path.write_text(f"{token}\n")
    await s.turn(prompt_for("Read the attached file and reply with its content only.", [], [path]))
    return {"P5": Observation(True, token in s.replies_since(mark))}


async def bash_turn(s: Stage) -> dict[str, Observation]:
    mark, marker = s.mark(), s.workdir / "probe-ran.txt"
    await s.turn(
        "Use the Bash tool to run exactly this command: echo ok > probe-ran.txt\nThen reply: done"
    )
    # A Bash call is known by the permission request the CLI sends for it, whatever its title.
    # The reply's tool line may show the command only while it runs, depending on update
    # timing; once folded it still names the tool (`✓ Bash`).
    called = "Bash" in s.asked_since(mark)
    detail = "" if called else f"permission requests: {s.asked_since(mark) or 'none'}"
    return {
        "P10": Observation(called, "Bash" in s.tool_lines_since(mark), detail),
        "P11": Observation(called, marker.exists(), detail),
    }


async def background_stop(s: Stage) -> dict[str, Observation]:
    await s.turn(
        "Use the Bash tool with run_in_background set to true to run: tail -f /dev/null\n"
        "Do not wait for it. Reply: started"
    )
    if not await until(lambda: "1 shell" in s.shown_now(), 20):
        return {"P12": Observation(False, False, "no background command started")}
    stopped = await s.session.stop()
    gone = await until(lambda: "1 shell" not in s.shown_now(), 30)
    detail = "" if stopped else "!stop found nothing to stop"
    detail = detail or ("" if gone else "the running line still shows the command after 30 s")
    return {"P12": Observation(True, stopped and gone, detail)}


def counted(text: str) -> set[int]:
    """The numbers written one per line: a sentence that names a number (`from 1 to 2000`)
    does not count as having written it."""
    return {int(line) for line in (x.strip() for x in text.splitlines()) if line.isdigit()}


async def interrupt(s: Stage) -> dict[str, Observation]:
    mark = s.mark()
    # Long enough that Haiku cannot finish it in the second or two a stop takes to arrive, since
    # the reply reaches Slack at most once per DEBOUNCE_SECONDS.
    turn = await s.session.submit(
        f"Without using any tool, reply with the numbers from 1 to {COUNT_TO}, one per line, "
        "and nothing else."
    )
    if not await until(lambda: 20 in counted(s.replies_since(mark)), 60):
        start = " ".join(s.replies_since(mark).split())[:80]
        return {"P8": Observation(False, False, f"the reply never reached 20: {start!r}")}
    stopped = await s.session.stop()
    ended = await until(turn.done.is_set, 60)
    finished = COUNT_TO in counted(s.replies_since(mark))
    if finished:
        detail = f"the reply ran to {COUNT_TO}: the interrupt did nothing"
    elif not stopped:
        detail = "!stop found no turn running"
    elif not ended:
        detail = "the turn did not end within 60 s of the stop"
    else:
        detail = ""
    return {"P8": Observation(True, stopped and ended and not finished, detail)}


async def resume(s: Stage, word: str) -> dict[str, Observation]:
    stored = s.state.get(CHANNEL)
    session_id = stored.session_id if stored else None
    if session_id is None:
        return {"P6": Observation(False, False, "no session id stored")}
    listed = await asyncio.to_thread(directory_sessions, s.workdir)
    picker = json.dumps(resume_blocks(s.workdir, listed, None, datetime.now(UTC)))
    in_list = any(i.session_id == session_id for i in listed) and session_id[:8] in picker
    seen = {"P6": Observation(True, in_list)}
    # A background command an earlier scene failed to stop keeps the channel busy.
    if not await until(lambda: s.session.idle, 30):
        return seen | {"P7": Observation(False, False, "the channel never became idle")}
    if not await s.manager.resume(CHANNEL, session_id):
        return seen | {"P7": Observation(False, False, "the resume was refused")}
    mark = s.mark()
    await s.turn("What was the code word I gave you? Reply with the word only.")
    return seen | {"P7": Observation(True, word in s.replies_since(mark))}


async def bypass(s: Stage) -> dict[str, Observation]:
    # `!status` shows the switch this daemon stored, whatever the CLI does with it, and the CLI's
    # server info keeps the mode the session started in (measured 2026-09-27 on 2.1.283): the
    # CLI's mode shows only as a command that runs with no permission request.
    await s.session.set_bypass(True)
    mark, marker = s.mark(), s.workdir / "bypass-ran.txt"
    try:
        await s.turn(
            "Use the Bash tool to run exactly this command: echo ok > bypass-ran.txt\n"
            "Then reply: done"
        )
    finally:
        await s.session.set_bypass(False)
    called = "Bash" in s.tool_lines_since(mark) and marker.exists()
    asked = s.asked_since(mark)
    return {
        "P9": Observation(
            called, not asked, f"asked for {asked}" if asked else "" if called else "no Bash call"
        )
    }


def folders(root: str) -> tuple[Path, Path]:
    """The session's working directory, named at random so its transcripts can be found and
    removed, and a folder outside it for attachments, as the daemon's uploads folder is."""
    base = Path(root).resolve()
    workdir, uploads = base / f"cws-probe-{secrets.token_hex(4)}", base / "uploads"
    workdir.mkdir()
    uploads.mkdir()
    return workdir, uploads


def forget_sessions(workdir: Path, log: Log) -> None:
    """Remove the transcripts the probe's sessions left under ~/.claude/projects: only the one
    folder named after this run's temporary directory, whose name is random. Anything else is
    left alone, and said."""
    projects = Path.home() / ".claude" / "projects"
    matches = [p for p in projects.glob(f"*{workdir.name}") if p.is_dir()]
    if len(matches) == 1:
        shutil.rmtree(matches[0])
    else:
        log(f"left the probe's transcripts in place: {len(matches)} folders match {workdir.name}")


# Each scene and the claims it observes: the one place a claim id meets its scene.
# `tests/test_probe.py` checks that together they cover `probe.claims.CLAIMS` exactly.
SCENES: dict[str, tuple[str, ...]] = {
    "first turn": ("P1", "P2", "P3"),
    "image": ("P4",),
    "file": ("P5",),
    "bash and approval": ("P10", "P11"),
    "background and stop": ("P12",),
    "interrupt": ("P8",),
    "resume": ("P6", "P7"),
    "bypass": ("P9",),
}


async def run_scenes(log: Log) -> dict[str, Observation]:
    seen: dict[str, Observation] = {}
    with tempfile.TemporaryDirectory(prefix="cws-probe-") as root:
        workdir, uploads = folders(root)
        s = Stage(workdir, log)
        word = secrets.token_hex(3)
        try:
            seen |= await attempt("first turn", s, first_turn(s, word))
            seen |= await attempt("image", s, image_turn(s))
            seen |= await attempt("file", s, file_turn(s, uploads))
            seen |= await attempt("bash and approval", s, bash_turn(s))
            seen |= await attempt("background and stop", s, background_stop(s))
            seen |= await attempt("interrupt", s, interrupt(s))
            seen |= await attempt("resume", s, resume(s, word))
            seen |= await attempt("bypass", s, bypass(s))
        finally:
            await s.manager.close_all()
            forget_sessions(workdir, log)
    return seen
