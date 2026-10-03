"""The scenes: each one causes the events a claim needs, through the daemon's own SessionManager.

Claude Code is real (the SDK's bundled CLI, the owner's login, Haiku); Slack is the test suite's
FakeSlack, since an SDK release changes nothing on the Slack side. What a claim checks is what the
owner would see: the text of the replies, the task cards, the footer, the approvals asked.
"""

import asyncio
import contextlib
import dataclasses
import json
import secrets
import shutil
import struct
import subprocess
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

from code_with_slack import texts
from code_with_slack.approvals import Approvals, Approve, Deny, Pending
from code_with_slack.attachments import prompt_for
from code_with_slack.footer import UsageCache
from code_with_slack.guards import Identity
from code_with_slack.resume import resume_blocks
from code_with_slack.sessions import (
    ClaudeClient,
    SessionDeps,
    SessionManager,
    ThreadSession,
    Turn,
    directory_sessions,
)
from code_with_slack.setup import Choice
from code_with_slack.state import StateStore
from probe.claims import Observation
from tests.fakes import FakeSlack, card_of

# The model the fixture recorder uses: the cheapest that runs every scene.
PROBE_MODEL = "claude-haiku-4-5-20251001"
CHANNEL, OWNER, TEAM, BOT = "C000PROBE", "U000PROBE", "T000PROBE", "U000PROBEBOT"
THREAD = "1700000000.000100"  # the probe's main thread, used by every scene but the two below
RESUME_THREAD = "1700000000.000200"  # the "resume" scene resumes P6/P7's session into this thread
MODEL_THREAD = "1700000000.000300"  # the new session the "model and effort resume" scene opens
MODEL_RESUME_THREAD = "1700000000.000301"  # its first resume, no effort stored: the losing half
MODEL_RESUME_THREAD2 = "1700000000.000302"  # its second resume, effort stored: the restoring half
SETUP_THREAD = "1700000000.000400"  # the new session the "setup model resume" scene opens
SETUP_RESUME_THREAD = "1700000000.000401"  # its resume, no model passed
TURN_LIMIT = 180.0
COUNT_TO = 2000
# What a background command's task card says while it runs (`renderer.BACKGROUND`).
RUNNING_CARD = "Running in background"


# Failures of the machine the probe runs on, not of the SDK: a turn past TURN_LIMIT, the network.
# Such a scene learned nothing, so its claims are UNPROVEN rather than BROKEN.
ENVIRONMENT = (TimeoutError, ConnectionError, OSError)
Log = Callable[[str], None]
CanUseTool = Callable[[str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResult]]


class ProbeApprovals(Approvals):
    """Answers every request as the owner would: Approve a tool, Deny a question."""

    def open(
        self,
        channel_id: str,
        thread_ts: str,
        title: str,
        questions: list[dict[str, Any]] | None = None,
    ) -> tuple[str, Pending]:
        approval_id, pending = super().open(channel_id, thread_ts, title, questions)
        decision = Deny() if questions else Approve()
        asyncio.get_running_loop().call_soon(
            self.resolve, approval_id, channel_id, thread_ts, decision
        )
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

    streams: int  # replies started
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
        # Session ids allowed to resume with no explicit `model=`: only the "model and effort
        # resume" scene's own (P15/P16). Whether a model passed at connect time, rather than set
        # with `/model` inside a turn, survives a resume with no `model=` sent again is not
        # itself measured anywhere in this codebase (`client_options`'s docstring only claims
        # this for a model set with `/model`); every other resume must keep forcing PROBE_MODEL,
        # or it risks silently reconnecting at the settings' own default model.
        self._model_free_resumes: set[str] = set()

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
        opened = self.manager.open(CHANNEL, THREAD)
        assert opened is not None

    def allow_model_free_resume(self, session_id: str) -> None:
        """Let `session_id` resume with no explicit `model=` on `client()`: the "model and
        effort resume" scene's own opt-in, once it knows its session id, so P15's resumed
        connect reflects the CLI's own remembered model rather than our own flag overriding it.
        No other scene's resume goes through this path."""
        self._model_free_resumes.add(session_id)

    def client(self, options: ClaudeAgentOptions) -> ClaudeClient:
        """The real client, on Haiku for every connect except a resume of a session
        `allow_model_free_resume` named: there, no explicit model is passed, so the CLI keeps
        whichever model that session last used, unclobbered by our own flag. Every other resume
        (the "resume" scene's P6/P7 included) keeps forcing Haiku exactly as before this session
        ever ran `/model`, since a model passed at connect surviving a resume with no `model=`
        sent again is not itself a measured fact."""
        decide: CanUseTool | None = options.can_use_tool

        async def noted(
            tool: str, tool_input: dict[str, Any], context: ToolPermissionContext
        ) -> PermissionResult:
            self.asked.append(tool)
            assert decide is not None
            return await decide(tool, tool_input, context)

        can_use_tool = noted if decide is not None else None
        model_free = options.resume is not None and options.resume in self._model_free_resumes
        model = None if model_free else PROBE_MODEL
        return ClaudeSDKClient(dataclasses.replace(options, model=model, can_use_tool=can_use_tool))

    @property
    def session(self) -> ThreadSession:
        session = self.manager.get(CHANNEL, THREAD)
        assert session is not None
        return session

    def mark(self) -> Mark:
        return Mark(len(self.slack.stream_ts), len(self.slack.calls), len(self.asked))

    def replies_since(self, mark: Mark) -> str:
        """The text of every reply started since `mark`: the turn's own, wherever an approval
        request or a notice came in between."""
        return "\n".join(self.slack.stream_texts()[mark.streams :])

    def cards_since(self, mark: Mark) -> str:
        """Every task card a reply showed since `mark`, as `status title output` a line each,
        in whatever state each had then."""
        seen: dict[str, str] = {}
        for method, args in self.slack.calls[mark.calls :]:
            for chunk in args.get("chunks") or []:
                if chunk["type"] == "task_update":
                    seen[chunk["id"]] = self.card_line(chunk)
            for block in args.get("blocks") or []:
                if method in ("chat.postMessage", "chat.update") and block["type"] == "task_card":
                    seen[block["task_id"]] = self.card_line(card_of(block))
        return "\n".join(seen.values())

    @staticmethod
    def card_line(card: dict[str, Any]) -> str:
        return f"{card['status']} {card['title']} {card.get('output', '')}".strip()

    def asked_since(self, mark: Mark) -> list[str]:
        return self.asked[mark.asked :]

    def shown_now(self) -> str:
        """What the channel shows now: each message's last blocks (the footer among them), deleted
        messages left out."""
        return "\n".join(
            json.dumps(m.blocks, ensure_ascii=False)
            for m in self.slack.messages.values()
            if not m.deleted
        )

    def cards_now(self) -> list[dict[str, Any]]:
        """The task cards of every message, as they show now."""
        return [card for cards in self.slack.message_cards() for card in cards]

    async def turn_on(self, session: ThreadSession, prompt: Any) -> Turn:
        turn = await session.submit(prompt)
        await asyncio.wait_for(turn.done.wait(), TURN_LIMIT)
        return turn

    async def turn(self, prompt: Any) -> Turn:
        return await self.turn_on(self.session, prompt)

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
    stored = s.state.thread(CHANNEL, THREAD)
    status = await s.session.status()
    return {
        "P1": Observation(True, s.session.cli_version is not None, f"cli {s.session.cli_version}"),
        "P3": Observation(True, bool(stored and stored.session_id)),
        "P2": Observation(True, "Context:" in status),
        "P17": models_listed(s.session.models),
    }


def models_listed(models: list[dict[str, Any]]) -> Observation:
    """P17: the setup message is built from these fields, so each entry must carry the first two
    and every entry that supports effort must list its levels."""
    named = bool(models) and all(m.get("value") and m.get("displayName") for m in models)
    levels = all(m.get("supportedEffortLevels") for m in models if m.get("supportsEffort"))
    detail = "" if named and levels else f"models: {[sorted(m) for m in models][:2]}"
    return Observation(True, named and levels, detail)


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
    # A Bash call is known by the permission request the CLI sends for it, whatever its title;
    # its card names the tool.
    called = "Bash" in s.asked_since(mark)
    detail = "" if called else f"permission requests: {s.asked_since(mark) or 'none'}"
    return {
        "P10": Observation(called, "Bash" in s.cards_since(mark), detail),
        "P11": Observation(called, marker.exists(), detail),
    }


async def previews(s: Stage) -> dict[str, Observation]:
    mark = s.mark()
    await s.turn(
        "One tool call per step. 1) Use the Write tool to create preview.txt with the lines "
        "alpha, beta and gamma. 2) Use the Edit tool on preview.txt to replace beta with delta. "
        "Then reply: done"
    )
    asked = s.asked_since(mark)
    called = "Write" in asked and "Edit" in asked
    lines = s.cards_since(mark)
    # Both previews built: the shapes they read are still the measured ones.
    shown = "Write(preview.txt)" in lines and "Update(preview.txt)" in lines
    shown = shown and "Wrote 3 lines" in lines and "Added 1 line, removed 1 line" in lines
    if not called:
        detail = f"permission requests: {asked}"
    elif not shown:
        # What the cards showed instead: a changed wording or shape is visible at once.
        detail = f"cards showed: {' '.join(lines.split())[:160]!r}"
    else:
        detail = ""
    return {"P13": Observation(called, shown, detail)}


async def background_stop(s: Stage) -> dict[str, Observation]:
    await s.turn(
        "Use the Bash tool with run_in_background set to true to run: tail -f /dev/null\n"
        "Do not wait for it. Reply: started"
    )

    def running() -> bool:
        return any(c.get("details") == RUNNING_CARD for c in s.cards_now())

    if not await until(running, 20):
        return {"P12": Observation(False, False, "no background command started")}
    stopped = await s.session.stop()
    gone = await until(lambda: not running(), 30)
    detail = "" if stopped else "!stop found nothing to stop"
    detail = detail or ("" if gone else "the card still shows the command running after 30 s")
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
    stored = s.state.thread(CHANNEL, THREAD)
    session_id = stored.session_id if stored else None
    if session_id is None:
        return {"P6": Observation(False, False, "no session id stored")}
    listed = await asyncio.to_thread(directory_sessions, s.workdir)
    # The picker as the daemon builds it (`handle_resume`): the first turn's thread holds this
    # session, so it takes no row and is counted in the line under the list.
    free = [i for i in listed if s.state.holder(i.session_id) is None]
    held = len(listed) - len(free)
    picker = json.dumps(
        resume_blocks(s.workdir, free, held, datetime.now(UTC), RESUME_THREAD),
        ensure_ascii=False,
    )
    counted_line = texts.RESUME_OPEN_ONE if held == 1 else texts.RESUME_OPEN_MANY.format(count=held)
    known = any(i.session_id == session_id for i in listed)
    in_picker = held >= 1 and counted_line in picker and session_id[:8] not in picker
    seen = {"P6": Observation(True, known and in_picker)}
    # A background command an earlier scene failed to stop keeps the channel busy.
    if not await until(lambda: s.session.idle, 30):
        return seen | {"P7": Observation(False, False, "the channel never became idle")}
    resumed = await s.manager.resume(CHANNEL, RESUME_THREAD, session_id)
    if resumed is None:
        return seen | {"P7": Observation(False, False, "the resume was refused")}
    mark = s.mark()
    await s.turn_on(resumed, "What was the code word I gave you? Reply with the word only.")
    return seen | {"P7": Observation(True, word in s.replies_since(mark))}


async def model_effort_resume(s: Stage) -> dict[str, Observation]:
    """P15: a resumed session keeps the model set with `/model`. P16: it loses the effort set
    with `/effort` (a resumed thread's `ThreadState` starts fresh, `state.open_thread`), and
    `ClaudeAgentOptions(effort=...)` restores it once the thread's own stored effort is set
    again before it connects."""
    session = s.manager.open(CHANNEL, MODEL_THREAD)
    assert session is not None
    await s.turn_on(session, "/model sonnet")
    await s.turn_on(session, "/effort low")
    await s.turn_on(session, "Reply with the single word: ok")
    stored = s.state.thread(CHANNEL, MODEL_THREAD)
    session_id = stored.session_id if stored else None
    await session.close()
    if session_id is None:
        detail = "no session id stored"
        return {"P15": Observation(False, False, detail), "P16": Observation(False, False, detail)}
    s.allow_model_free_resume(session_id)

    lost = await s.manager.resume(CHANNEL, MODEL_RESUME_THREAD, session_id)
    if lost is None:
        detail = "the resume was refused"
        return {"P15": Observation(False, False, detail), "P16": Observation(False, False, detail)}
    await s.turn_on(lost, "Reply with the single word: ok")
    status = await lost.status()
    model_kept = "sonnet" in status.lower()
    effort_lost = "Effort: `low`" not in status
    await lost.close()

    restored = await s.manager.resume(CHANNEL, MODEL_RESUME_THREAD2, session_id)
    if restored is None:
        detail = "the second resume was refused"
        return {
            "P15": Observation(True, model_kept, "" if model_kept else status[:160]),
            "P16": Observation(False, False, detail),
        }
    s.state.set_effort(CHANNEL, MODEL_RESUME_THREAD2, "low")
    await s.turn_on(restored, "Reply with the single word: ok")
    restored_status = await restored.status()
    effort_restored = "Effort: `low`" in restored_status
    await restored.close()

    p16_holds = effort_lost and effort_restored
    detail16 = (
        ""
        if p16_holds
        else f"after the plain resume: {status[:160]!r}; after the effort resume: "
        f"{restored_status[:160]!r}"
    )
    return {
        "P15": Observation(True, model_kept, "" if model_kept else status[:160]),
        "P16": Observation(True, p16_holds, detail16),
    }


async def setup_model_resume(s: Stage) -> dict[str, Observation]:
    """P18: a model set with `set_model()` (what the setup's Start does) survives a resume that
    passes none. The session's connect forces Haiku, so Sonnet showing after the resume can only
    come from the session's own record."""
    session = s.manager.open(CHANNEL, SETUP_THREAD)
    assert session is not None
    await session.apply_setup(Choice(model="sonnet"))
    await s.turn_on(session, "Reply with the single word: ok")
    stored = s.state.thread(CHANNEL, SETUP_THREAD)
    session_id = stored.session_id if stored else None
    await session.close()
    if session_id is None:
        return {"P18": Observation(False, False, "no session id stored")}
    s.allow_model_free_resume(session_id)
    resumed = await s.manager.resume(CHANNEL, SETUP_RESUME_THREAD, session_id)
    if resumed is None:
        return {"P18": Observation(False, False, "the resume was refused")}
    await s.turn_on(resumed, "Reply with the single word: ok")
    status = await resumed.status()
    await resumed.close()
    kept = "sonnet" in status.lower()
    return {"P18": Observation(True, kept, "" if kept else status[:160])}


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
    called = "Bash" in s.cards_since(mark) and marker.exists()
    asked = s.asked_since(mark)
    return {
        "P9": Observation(
            called, not asked, f"asked for {asked}" if asked else "" if called else "no Bash call"
        )
    }


def init_repo(path: Path, branch: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)


async def working_folder(s: Stage) -> dict[str, Observation]:
    # Last: the session stays in the child folder, where the other scenes' files do not go.
    app, branch = s.workdir / "app", f"probe-{secrets.token_hex(3)}"
    init_repo(app, branch)
    mark = s.mark()
    await s.turn("Use the Bash tool to run exactly this command: cd app\nThen reply: done")
    # A `cd` inside the working folder asks for no approval (measured 2026-09-27, 2.1.283):
    # the call shows on its card only.
    called = "Bash" in s.cards_since(mark)
    moved = s.session.working_directory == app
    shown = branch in s.shown_now()
    detail = f"working directory {s.session.working_directory}, branch shown: {shown}"
    return {"P14": Observation(called, moved and shown, "" if moved and shown else detail)}


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
    "first turn": ("P1", "P2", "P3", "P17"),
    "image": ("P4",),
    "file": ("P5",),
    "bash and approval": ("P10", "P11"),
    "previews": ("P13",),
    "background and stop": ("P12",),
    "interrupt": ("P8",),
    "resume": ("P6", "P7"),
    "model and effort resume": ("P15", "P16"),
    "setup model resume": ("P18",),
    "bypass": ("P9",),
    "working folder": ("P14",),
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
            seen |= await attempt("previews", s, previews(s))
            seen |= await attempt("background and stop", s, background_stop(s))
            seen |= await attempt("interrupt", s, interrupt(s))
            seen |= await attempt("resume", s, resume(s, word))
            seen |= await attempt("model and effort resume", s, model_effort_resume(s))
            seen |= await attempt("setup model resume", s, setup_model_resume(s))
            seen |= await attempt("bypass", s, bypass(s))
            seen |= await attempt("working folder", s, working_folder(s))
        finally:
            await s.manager.close_all()
            forget_sessions(workdir, log)
    return seen
