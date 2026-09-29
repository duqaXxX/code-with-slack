"""The Slack side: every inbound path, each checked on its own before it reaches a session."""

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import SDKSessionInfo
from slack_bolt.async_app import AsyncAck, AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.approvals import (
    QUESTION_FORM,
    Answer,
    Approvals,
    Approve,
    Decision,
    Deny,
    Draft,
    absorb,
    answered_blocks,
    draft_answers,
    first_unanswered,
    is_answered,
    question_view,
)
from code_with_slack.attachments import (
    DownloadFailed,
    download,
    images_refusal,
    is_image,
    limit_for,
    prompt_for,
    refusal,
    save,
)
from code_with_slack.commands import (
    Bind,
    Bypass,
    Guide,
    Help,
    Invalid,
    Passthrough,
    Resume,
    Status,
    Stop,
    Word,
    help_text,
    parse_bang,
)
from code_with_slack.config import Config
from code_with_slack.folders import BIND_ACTION, bind_blocks
from code_with_slack.guards import (
    ChannelGuard,
    Identity,
    interaction_actor,
    is_owner,
    is_prompt_message,
    message_actor,
)
from code_with_slack.hold import HOLD_CANCEL, HOLD_CONTINUE, Holds, hold_blocks
from code_with_slack.prompt import Prompt
from code_with_slack.render.escape import markdown_escape, mrkdwn_escape
from code_with_slack.render.renderer import one_line
from code_with_slack.render.sinks import (
    FALLBACK_LIMIT,
    context_block,
    delete_request,
    describe,
    notice_text,
    split,
)
from code_with_slack.resume import RESUME_ACTION, RESUME_ROWS, TITLE_LIMIT, matching, resume_blocks
from code_with_slack.sessions import (
    DirectoryUnavailable,
    SessionClosed,
    SessionGone,
    SessionManager,
    ThreadSession,
    resolve_directory,
)
from code_with_slack.state import StateStore

logger = logging.getLogger(__name__)
DECISION_ACTIONS = ("approval_allow", "approval_deny", "question_skip")


# Slack sends a link as <url|label> or <url> (message formatting reference, read 2026-09-25).
# Mentions (<@U…>, <#C…>) stay as sent: naming them would need a scope the app does not have.
LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]+))?>")
SCHEME = re.compile(r"^(?:https?://|mailto:)")


def _link(match: re.Match[str]) -> str:
    url, label = match[1], match[2]
    if label is None:
        return url
    # A link Slack made from a typed name carries that name as its label; one the owner named
    # keeps its address, which Claude needs to open it.
    return label if SCHEME.sub("", url) == label else f"{label} ({url})"


def slack_unescape(text: str) -> str:
    """The text as the owner typed it, with the address of any link the owner named."""
    text = LINK.sub(_link, text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def bound_text(
    directory: Path, unavailable: DirectoryUnavailable | None, old_folders: list[Path]
) -> str:
    """The answer to a bind: what keeps a session from starting in the folder, if anything
    (D5's BIND_UNAVAILABLE keeps priority: what would stop every session there matters more than
    where an existing thread's session lives), else the old folders existing threads keep working
    in, if any."""
    if unavailable is not None:
        return texts.BIND_UNAVAILABLE.format(
            directory=mrkdwn_escape(str(directory)), reason=unavailable.message
        )
    if not old_folders:
        return texts.BIND_OK.format(directory=mrkdwn_escape(str(directory)))
    old = ", ".join(f"`{mrkdwn_escape(str(folder))}`" for folder in old_folders)
    return texts.BIND_OK_ELSEWHERE.format(directory=mrkdwn_escape(str(directory)), old=old)


def click_thread(body: dict[str, Any]) -> str:
    """The thread a button or a form sits in: `container.thread_ts`, falling back to
    `message.thread_ts` then the message's own ts (a button is never trusted, so the thread it
    answers in is read the same way for every kind of click)."""
    container = body.get("container") or {}
    message = body.get("message") or {}
    thread_ts = container.get("thread_ts") or message.get("thread_ts") or message.get("ts")
    return str(thread_ts)


def is_clear(command: Passthrough) -> bool:
    """Whether a passthrough is `!clear`: refused inside a thread (one thread is one session),
    left as an ordinary passthrough everywhere else."""
    return command.text.split(" ", 1)[0].lower() == "clear"


class Fetch(Protocol):
    """How a file is downloaded: its URL, its declared type and the size limit, to its bytes.
    Keywords only: the two strings must never be swapped."""

    async def __call__(self, *, url: str, mimetype: str, limit: int) -> bytes: ...


def build_app(
    *,
    slack: AsyncWebClient,
    config: Config,
    identity: Identity,
    sessions: SessionManager,
    approvals: Approvals,
    holds: Holds,
    guard: ChannelGuard,
    state: StateStore,
    uploads: Path,
    fetch: Fetch | None = None,
) -> AsyncApp:
    async def download_file(*, url: str, mimetype: str, limit: int) -> bytes:
        return await download(url, config.bot_token, mimetype, limit)

    fetch_file = fetch or download_file
    # One lock per thread, alive only while someone holds or waits on it (a channel keeps
    # opening new threads for as long as it runs; a lock kept forever would leak).
    arrival_order: dict[tuple[str, str], asyncio.Lock] = {}
    arrival_waiters: dict[tuple[str, str], int] = {}
    # D5: threads already told (or opened) since this process started, so the old-folder notice
    # (plan 7) shows once per thread per process, not on every reply.
    old_folder_notified: set[tuple[str, str]] = set()

    @contextlib.asynccontextmanager
    async def arrival_lock(key: tuple[str, str]) -> AsyncIterator[None]:
        arrival_waiters[key] = arrival_waiters.get(key, 0) + 1
        lock = arrival_order.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                yield
        finally:
            arrival_waiters[key] -= 1
            if arrival_waiters[key] == 0:
                del arrival_waiters[key]
                del arrival_order[key]

    async def authorize() -> AuthorizeResult:
        # auth.test already ran at startup; who may act is decided by the guards below.
        return AuthorizeResult(
            enterprise_id=None,
            team_id=identity.team_id,
            bot_user_id=identity.bot_user_id,
            bot_token=config.bot_token,
        )

    app = AsyncApp(client=slack, authorize=authorize)

    async def tell_owner(channel: str, thread_ts: str | None, text: str) -> None:
        try:
            await slack.chat_postEphemeral(
                channel=channel,
                thread_ts=thread_ts,
                user=identity.owner_user_id,
                text=text,
                blocks=[context_block(notice_text(text))],
            )
        except Exception as exc:
            logger.warning("could not reach the owner in %s: %s", channel, describe(exc))

    async def reply_on_failure(channel: str, thread_ts: str, work: Awaitable[None]) -> None:
        """A failure after the checks reaches the owner as a line of its own, never as silence."""
        try:
            await work
        except (DirectoryUnavailable, SessionClosed, SessionGone) as exc:
            await tell_owner(channel, thread_ts, exc.message)
        except Exception as exc:
            logger.error("a request failed in %s/%s: %s", channel, thread_ts, type(exc).__name__)
            await tell_owner(channel, thread_ts, texts.ERROR_REPLY.format(error=type(exc).__name__))

    async def admitted(
        user: str | None, team: str | None, channel: str | None, thread_ts: str | None
    ) -> bool:
        if not channel or not is_owner(identity, user, team):
            logger.info("ignored an inbound event from someone other than the owner")
            return False
        reason = await guard.refusal(channel)
        if reason is not None:
            await tell_owner(channel, thread_ts, texts.CHANNEL_REFUSED.format(reason=reason))
            return False
        return True

    async def notice(channel: str, thread_ts: str, text: str) -> None:
        """One of the daemon's own notices (a bind, a resume, a restart), small and grey as the
        footer, so it reads apart from Claude's replies. `text` is mrkdwn."""
        await slack.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=text[:FALLBACK_LIMIT],
            blocks=[context_block(notice_text(text))],
            unfurl_links=False,
            unfurl_media=False,
        )

    async def say(channel: str, thread_ts: str, text: str) -> None:
        """A reference the owner reads (`!help`, `!guide`, `!status`), at full size in the
        thread: a long one continues in a new message past a markdown block's limit."""
        for chunk in split(text):
            await slack.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=chunk[:FALLBACK_LIMIT],
                blocks=[{"type": "markdown", "text": chunk}],
                unfurl_links=False,
                unfurl_media=False,
            )

    @app.event("message")
    async def on_message(event: dict[str, Any]) -> None:
        if not is_prompt_message(event):
            return
        user, team = message_actor(event)
        channel = event.get("channel")
        ts = str(event.get("ts"))
        thread_ts = str(event.get("thread_ts") or ts)
        if not await admitted(user, team, channel, thread_ts):
            return
        assert channel is not None
        await reply_on_failure(channel, thread_ts, handle_message(channel, thread_ts, ts, event))

    async def handle_message(channel: str, thread_ts: str, ts: str, event: dict[str, Any]) -> None:
        top_level = thread_ts == ts
        text = slack_unescape(event.get("text") or "")
        files: list[dict[str, Any]] = event.get("files") or []
        # A message with files is a prompt: no daemon word or command takes a file.
        command = None if files else parse_bang(text)
        # A known session's thread routes a word to it; anywhere else (truly top-level, or a
        # thread that is not a session) a word acts exactly as a top-level one would.
        session = None if top_level else sessions.get(channel, thread_ts)
        if isinstance(command, Word):
            await handle_word(channel, thread_ts, command, session=session)
            return
        if session is not None:
            await old_folder_notice(channel, thread_ts, session)
            await submit_to_session(
                channel, thread_ts, session, text, files, command, in_thread=True
            )
            return
        if not top_level:
            # Anything but a daemon word, in a thread that holds no session: nowhere to send it.
            await tell_owner(channel, thread_ts, texts.NOT_A_SESSION)
            return
        opened = sessions.open(channel, thread_ts)
        if opened is None:
            await tell_owner(channel, thread_ts, texts.UNBOUND.format(root=config.allowed_root))
            return
        await submit_to_session(channel, thread_ts, opened, text, files, command, in_thread=False)

    async def old_folder_notice(channel: str, thread_ts: str, session: ThreadSession) -> None:
        """D5: a thread whose folder differs from the channel's current one, told once per
        process (a thread told before this process started, or opened during it, needs no more).
        A failed post must not drop the prompt that follows it: logged (ids only) and left
        unmarked, so the next message in this thread tries the notice again rather than the
        owner losing it for the rest of the process."""
        key = (channel, thread_ts)
        record = state.channel(channel)
        if key in old_folder_notified or record is None or session.directory == record.directory:
            return
        try:
            await notice(
                channel,
                thread_ts,
                texts.OLD_THREAD_FOLDER.format(
                    old=mrkdwn_escape(str(session.directory)),
                    new=mrkdwn_escape(str(record.directory)),
                ),
            )
        except Exception as exc:
            logger.warning(
                "could not post the old-folder notice in %s/%s: %s",
                channel,
                thread_ts,
                describe(exc),
            )
            return
        old_folder_notified.add(key)

    async def submit_to_session(
        channel: str,
        thread_ts: str,
        session: ThreadSession,
        text: str,
        files: list[dict[str, Any]],
        command: Passthrough | None,
        *,
        in_thread: bool,
    ) -> None:
        if in_thread and isinstance(command, Passthrough) and is_clear(command):
            await notice(channel, thread_ts, texts.CLEAR_IN_THREAD)
            return
        # Prompts and commands for Claude Code enter the queue in the order they were sent,
        # although files take a while to download. Every reply goes to the thread.
        async with arrival_lock((channel, thread_ts)):
            prompt = await with_attachments(channel, thread_ts, text, files) if files else text
            if prompt is None:
                return
            # Checked before D8 too: a drain already cancelled every hold open when it started
            # (SessionManager.drain) and will never cancel one opened after, so a message that
            # arrives once draining has begun must never open a new one (it would wait forever).
            if sessions.draining:
                await notice(channel, thread_ts, texts.RESTARTING)
                return
            # D8: this message would wake `session` (busy just queues behind what is already
            # running); ask first when another live session, of any channel, is already busy in
            # the same resolved folder. Later messages of this thread queue behind the wait,
            # since it runs inside the arrival lock. `held` is the object a Continue's ✋ was left
            # standing on (`hold_end(continued=True)` bets on the `submit()` below to replace it
            # with ⏳): kept apart from `session`, which a `SessionClosed` retry below can
            # reassign, so a non-submit exit always restores the reaction on the object that
            # actually shows it.
            held: ThreadSession | None = None
            if not session.busy:
                other = sessions.working_in(besides=session)
                if other is not None:
                    if not await hold_before_sending(channel, thread_ts, session, other):
                        return
                    held = session
            # Retried once against a freshly looked-up session: the one this call was handed can
            # still close under it (most likely D9's idle close, though `touch()` at the lookup
            # already guards the common case) during the download above or the steps below.
            submitted = failed = False
            try:
                for attempt in range(2):
                    try:
                        if isinstance(command, Passthrough):
                            await session.ensure_connected()
                            known = {str(c.get("name")) for c in session.commands}
                            name = command.text.split(" ", 1)[0]
                            prompt = f"/{command.text}" if name in known else text
                        # Checked last, with no await before the submit: a stop can start during a
                        # download. The daemon's words still work meanwhile (`!stop` shortens the
                        # wait); a new turn would not finish, and Slack does not resend this event.
                        if sessions.draining:
                            await notice(channel, thread_ts, texts.RESTARTING)
                            return
                        await session.submit(prompt)
                        submitted = True
                        return
                    except SessionClosed:
                        if attempt:
                            raise
                        fresh = sessions.get(channel, thread_ts)
                        if fresh is None:
                            # Not just closed: its thread's own entry is gone too (D7's
                            # SessionGone close), so a retry would find nothing here again either.
                            raise SessionGone from None
                        session = fresh
            except BaseException:
                failed = True
                raise
            finally:
                # `submitted` alone, not a fixed set of exception types: `ensure_connected` can
                # also raise `ResultError` (a logged-out CLI, most likely) or anything a stray
                # bug throws, and `notice(RESTARTING)` above can itself fail; none of them may
                # ever leave a Continue's ✋ standing forever. A failure gets the reaction a turn
                # that reached the queue and then failed gets; a plain return (the drain notice
                # posted fine) restores a Cancel's own reaction instead.
                if held is not None and not submitted:
                    await held.react_hold_abandoned(error=failed)

    async def hold_before_sending(
        channel: str, thread_ts: str, session: ThreadSession, other: ThreadSession
    ) -> bool:
        """D8: post `Another session is working in this folder: <link>. Send anyway?` and wait
        for the owner's Continue or Cancel, cancelled the same way by `!stop` (in this thread or
        the whole channel) or a drain. True to send the message on; False when it was not,
        either way telling the owner `Not sent.` already."""
        link = await thread_mrkdwn_link(other.channel_id, other.thread_ts, "Session")
        if sessions.draining:  # a restart could have started during the permalink call above
            await notice(channel, thread_ts, texts.RESTARTING)
            return False
        hold_id, pending = holds.open(channel, thread_ts)
        try:
            posted = await slack.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=texts.HOLD_QUESTION.format(link=link),
                blocks=hold_blocks(hold_id, link),
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception as exc:
            # Nobody can answer a question that was never shown: fail closed, as an unpostable
            # approval does, rather than send into a folder another session is using.
            logger.error("could not post a D8 hold in %s/%s: %s", channel, thread_ts, describe(exc))
            holds.discard(hold_id)
            await tell_owner(channel, thread_ts, texts.HOLD_UNPOSTED)
            return False
        message_ts = str(posted["ts"])
        if holds.posted(hold_id, message_ts):
            # Crash repair (issue #19): a D8 hold question is a request like an approval.
            # Best-effort (fix round 2 item 1): outside the try/finally below on purpose, so a
            # failed write here can never skip `hold_start`/`hold_end` and leave the hold itself
            # undiscarded; the message is already live either way, so a failure is logged loudly.
            try:
                state.add_request(channel, thread_ts, message_ts)
            except Exception as exc:
                logger.error(
                    "posted a D8 hold in %s/%s that state.json could not record: %s",
                    channel,
                    thread_ts,
                    describe(exc),
                )
            continued = False
            try:
                session.hold_start()
                continued = await pending.future
            finally:
                await session.hold_end(continued=continued)
                holds.discard(hold_id)  # a no-op once resolved; catches a cancelled wait
        else:
            # Decided (a fast `!stop` or drain) before the message's own ts was known: nobody
            # else learned it in time to remove it, and the wait below is already over, so
            # `hold_start`/`hold_end` (and their ✋) never ran for it either.
            await remove_request(channel, thread_ts, message_ts)
            continued = pending.future.result()
        if not continued:
            await notice(channel, thread_ts, texts.NOT_SENT)
        return continued

    async def on_hold_decision(ack: AsyncAck, body: dict[str, Any]) -> None:
        await ack()
        user, team = interaction_actor(body)
        channel = (body.get("channel") or {}).get("id")
        thread_ts = click_thread(body)
        if not await admitted(user, team, channel, thread_ts):
            return
        assert channel is not None
        action = body["actions"][0]
        hold_id = str(action.get("value"))
        continue_ = action["action_id"] == HOLD_CONTINUE
        if holds.resolve(hold_id, channel, thread_ts, continue_=continue_) is None:
            await tell_owner(channel, thread_ts, texts.HOLD_GONE)
            return
        await remove_request(channel, thread_ts, body["message"]["ts"])

    for action_id in (HOLD_CONTINUE, HOLD_CANCEL):
        app.action(action_id)(on_hold_decision)

    async def handle_word(
        channel: str, thread_ts: str, command: Word, *, session: ThreadSession | None
    ) -> None:
        match command:
            case Help() | Invalid():
                # A mistyped word gets the full list, which shows how each word is written.
                query = command.query if isinstance(command, Help) else ""
                if session is not None:
                    await session.ensure_connected()
                commands = session.commands if session else None
                await say(channel, thread_ts, help_text(commands, query))
            case Guide():
                await say(channel, thread_ts, texts.GUIDE)
            case Bind():
                if session is not None:
                    await notice(channel, thread_ts, texts.WORD_IN_THREAD.format(word=command.WORD))
                elif command.path:
                    await bind_to(channel, thread_ts, command.path)
                else:
                    await list_folders(channel, thread_ts)
            case Bypass(on=on):
                if session is None:
                    await notice(channel, thread_ts, texts.BYPASS_TOP_LEVEL)
                else:
                    await session.set_bypass(on)
                    await notice(
                        channel,
                        thread_ts,
                        texts.BYPASS_ON
                        if on
                        else texts.BYPASS_OFF.format(mode=session.native_mode),
                    )
            case Status():
                if session is None:
                    await channel_status(channel, thread_ts)
                else:
                    await say(channel, thread_ts, await session.status())
            case Stop():
                if session is None:
                    stopped = await sessions.stop_channel(channel)
                    # None: only a D8 hold was cancelled somewhere in the channel (`Not sent.`,
                    # from its own waiter); no second notice, since nothing Claude Code was doing
                    # stopped.
                    if stopped is not None:
                        await notice(
                            channel,
                            thread_ts,
                            texts.STOPPED_CHANNEL if stopped else texts.NOTHING_TO_STOP,
                        )
                else:
                    thread_stopped = await session.stop()
                    # None: only a D8 hold was here, cancelled already (`Not sent.`, from the
                    # waiter); no second notice, since nothing Claude Code was doing stopped.
                    if thread_stopped is not None:
                        await notice(
                            channel,
                            thread_ts,
                            texts.STOPPED_THREAD
                            if thread_stopped
                            else texts.NOTHING_TO_STOP_THREAD,
                        )
            case Resume(target=target):
                if session is not None:
                    await notice(channel, thread_ts, texts.WORD_IN_THREAD.format(word=command.WORD))
                else:
                    await handle_resume(channel, thread_ts, target)

    async def channel_status(channel: str, thread_ts: str) -> None:
        record = state.channel(channel)
        if record is None:
            await tell_owner(channel, thread_ts, texts.UNBOUND.format(root=config.allowed_root))
            return
        lines = [texts.STATUS_CHANNEL_HEADER.format(directory=record.directory)]
        live = sessions.sessions_of(channel)
        if not live:
            lines.append(texts.STATUS_CHANNEL_EMPTY)
        else:
            lines.extend(
                await asyncio.gather(
                    *(
                        channel_status_row(channel, record.directory, live_session)
                        for live_session in live
                    )
                )
            )
        await say(channel, thread_ts, "\n".join(lines))

    async def channel_status_row(
        channel: str, channel_directory: Path, session: ThreadSession
    ) -> str:
        link = await thread_link(channel, session.thread_ts)
        if session.waiting_for_owner:
            activity = texts.STATUS_CHANNEL_WAITING
        elif session.busy or session.running_kinds:
            # A session with only a background task running is not idle either.
            activity = texts.STATUS_CHANNEL_BUSY
        else:
            activity = texts.STATUS_CHANNEL_IDLE
        row = texts.STATUS_CHANNEL_ROW.format(link=link, activity=activity)
        if session.running_kinds:
            row += f" · {texts.RUNNING.format(counts=session.running_kinds)}"
        if session.bypass:
            row += texts.STATUS_CHANNEL_BYPASS
        if session.directory != channel_directory:
            row += texts.STATUS_CHANNEL_FOLDER.format(directory=session.directory)
        return row

    async def _permalink(channel: str, thread_ts: str) -> str | None:
        try:
            return str(
                (await slack.chat_getPermalink(channel=channel, message_ts=thread_ts))["permalink"]
            )
        except Exception as exc:
            logger.warning(
                "could not get a permalink for %s/%s: %s", channel, thread_ts, describe(exc)
            )
            return None

    async def thread_link(channel: str, thread_ts: str) -> str:
        # `say` posts a markdown block: standard Markdown links (docs.slack.dev, markdown
        # block), not mrkdwn's `<url|label>`.
        permalink = await _permalink(channel, thread_ts)
        if permalink is None:
            return texts.STATUS_CHANNEL_LINK_FALLBACK.format(thread_ts=thread_ts)
        return f"[Session]({permalink})"

    async def thread_mrkdwn_link(channel: str, thread_ts: str, label: str) -> str:
        # A resume row and a notice are mrkdwn (`context_block`), which takes `<url|label>`.
        permalink = await _permalink(channel, thread_ts)
        if permalink is None:
            return texts.STATUS_CHANNEL_LINK_FALLBACK.format(thread_ts=thread_ts)
        return f"<{permalink}|{label}>"

    @app.action("answer")
    async def on_answer(ack: AsyncAck) -> None:
        await ack()  # a menu inside a question: its value is read when Submit is clicked

    async def on_decision(ack: AsyncAck, body: dict[str, Any]) -> None:
        await ack()
        user, team = interaction_actor(body)
        channel = (body.get("channel") or {}).get("id")
        thread_ts = click_thread(body)
        if not await admitted(user, team, channel, thread_ts):
            return
        assert channel is not None
        action = body["actions"][0]
        approval_id = str(action.get("value"))
        pending = approvals.get(approval_id)
        if pending is None or pending.channel_id != channel or pending.thread_ts != thread_ts:
            await tell_owner(channel, thread_ts, texts.APPROVAL_GONE)
            return
        decision: Decision = Approve() if action["action_id"] == "approval_allow" else Deny()
        if approvals.resolve(approval_id, channel, thread_ts, decision) is None:
            await tell_owner(channel, thread_ts, texts.APPROVAL_GONE)
            return
        await remove_request(channel, thread_ts, body["message"]["ts"])

    for action_id in DECISION_ACTIONS:
        app.action(action_id)(on_decision)

    async def with_attachments(
        channel: str, thread_ts: str, text: str, files: list[dict[str, Any]]
    ) -> Prompt | None:
        """The prompt for `text` and its files; None, after telling the owner why, when any file
        cannot reach Claude: the message is sent whole or not at all."""

        async def refuse(file: dict[str, Any], reason: str) -> None:
            name = mrkdwn_escape(str(file.get("name") or file.get("id")))
            await notice(channel, thread_ts, texts.UPLOAD_FAILED.format(name=name, reason=reason))

        for file in files:
            reason = refusal(file)
            if reason is not None:
                await refuse(file, reason)
                return None
        together = images_refusal(files)
        if together is not None:
            await notice(channel, thread_ts, together)
            return None
        fetched = await asyncio.gather(
            *(
                fetch_file(
                    url=str(f["url_private_download"]),
                    mimetype=str(f.get("mimetype")),
                    limit=limit_for(f),
                )
                for f in files
            ),
            return_exceptions=True,
        )
        # Nothing is saved until every file arrived: a failed message leaves no copy behind.
        for file, data in zip(files, fetched, strict=True):
            if isinstance(data, DownloadFailed):
                await refuse(file, texts.UPLOAD_DOWNLOAD.format(error=data))
                return None
            if isinstance(data, BaseException):
                raise data
        images: list[tuple[str, bytes]] = []
        paths: list[Path] = []
        for file, data in zip(files, fetched, strict=True):
            assert isinstance(data, bytes)
            if is_image(file):
                images.append((str(file["mimetype"]), data))
            else:
                try:
                    paths.append(await save(uploads, file, data))
                except DownloadFailed as exc:
                    await refuse(file, str(exc))
                    return None
        return prompt_for(text, images, paths)

    async def bind_to(channel: str, thread_ts: str, path: str) -> None:
        directory = await folder_named(channel, thread_ts, path)
        if directory is None:
            return
        if not await sessions.bind(channel, directory):
            await notice(channel, thread_ts, texts.BIND_BUSY)
            return
        await announce_bind(channel, thread_ts, directory)

    async def announce_bind(channel: str, thread_ts: str, directory: Path) -> None:
        # Thread entries keep their own folder either side of the bind (state.bind), so the old
        # folders (D5) can be read now: those of the channel's threads that differ from the new one.
        unavailable = await sessions.unavailable(directory)
        record = state.channel(channel)
        threads = record.threads.values() if record is not None else []
        old_folders = sorted({t.directory for t in threads if t.directory != directory}, key=str)
        await notice(channel, thread_ts, bound_text(directory, unavailable, old_folders))

    async def folder_named(channel: str, thread_ts: str, path: str) -> Path | None:
        directory = resolve_directory(path, config.allowed_root)
        if directory is None:
            root = mrkdwn_escape(str(config.allowed_root))
            await notice(
                channel, thread_ts, texts.BIND_OUTSIDE.format(path=mrkdwn_escape(path), root=root)
            )
        return directory

    async def list_folders(channel: str, thread_ts: str) -> None:
        root = config.allowed_root
        record = state.channel(channel)
        folders = await sessions.folders_in(root)
        blocks = bind_blocks(root, folders, record.directory if record else None)
        await slack.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(texts.BIND_LIST if folders else texts.BIND_EMPTY).format(root=root),
            blocks=blocks,
            unfurl_links=False,
            unfurl_media=False,
        )

    @app.action(BIND_ACTION)
    async def on_bind(ack: AsyncAck, body: dict[str, Any]) -> None:
        await ack()
        user, team = interaction_actor(body)
        channel = (body.get("channel") or {}).get("id")
        thread_ts = click_thread(body)
        if not await admitted(user, team, channel, thread_ts):
            return
        assert channel is not None
        await reply_on_failure(channel, thread_ts, bind_clicked(channel, body))

    async def bind_clicked(channel: str, body: dict[str, Any]) -> None:
        thread_ts = click_thread(body)
        # The button is not trusted: its folder goes through the same check as a typed `!bind`.
        directory = await folder_named(channel, thread_ts, str(body["actions"][0].get("value")))
        if directory is None:
            return
        record = state.channel(channel)
        if record is not None and record.directory == directory:
            shown = mrkdwn_escape(str(directory))
            await notice(channel, thread_ts, texts.BIND_ALREADY.format(directory=shown))
            return
        # A list can be old: unlike a typed `!bind`, a click never ends work in flight either.
        if not await sessions.bind(channel, directory):
            await notice(channel, thread_ts, texts.BIND_BUSY)
            return
        await announce_bind(channel, thread_ts, directory)
        await remove_request(channel, thread_ts, body["message"]["ts"])

    async def handle_resume(channel: str, thread_ts: str, target: str) -> None:
        record = state.channel(channel)
        if record is None:
            await tell_owner(channel, thread_ts, texts.UNBOUND.format(root=config.allowed_root))
            return
        directory = record.directory
        # Only the list shows dates: matching a target needs none, and dating reads every file.
        stored = await sessions.sessions_in(directory, dated=not target)
        if not target:
            # A permalink is a Slack round trip: fetched only for the held rows the list shows.
            candidates = [(s.session_id, state.holder(s.session_id)) for s in stored[:RESUME_ROWS]]
            held = [(sid, holder) for sid, holder in candidates if holder is not None]
            links = dict(
                zip(
                    (sid for sid, _ in held),
                    await asyncio.gather(
                        *(
                            thread_mrkdwn_link(holder[0], holder[1], "open elsewhere")
                            for _, holder in held
                        )
                    ),
                    strict=True,
                )
            )
            blocks = resume_blocks(
                directory,
                stored,
                lambda sid: links.get(sid),
                datetime.now().astimezone(),
            )
            await slack.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=texts.RESUME_LIST.format(directory=directory),
                blocks=blocks,
                unfurl_links=False,
                unfurl_media=False,
            )
            return
        found = matching(stored, target)
        if len(found) != 1:
            template = texts.RESUME_AMBIGUOUS if found else texts.RESUME_NONE
            await notice(
                channel,
                thread_ts,
                template.format(
                    directory=mrkdwn_escape(str(directory)), target=mrkdwn_escape(target)
                ),
            )
            return
        await resume_into_new_thread(channel, thread_ts, directory, found[0])

    async def resume_into_new_thread(
        channel: str, thread_ts: str, directory: Path, chosen: SDKSessionInfo
    ) -> bool:
        """Point a new thread at `chosen`, a session read from `directory`; False, after telling
        the owner why, when nothing changed: this thread already holds a session (a resume is
        never a swap), `chosen` is already held by some other thread (D6: one session lives in
        one thread), or the channel was bound to another folder while `chosen` was read from
        `directory`. Every check runs with no `await` before the `resume` they guard, so nothing
        can change between the checks and the call they protect."""
        if sessions.get(channel, thread_ts) is not None:
            await tell_owner(channel, thread_ts, texts.RESUME_HELD)
            return False
        holder = state.holder(chosen.session_id)
        if holder is not None:
            link = await thread_mrkdwn_link(holder[0], holder[1], "Session")
            await tell_owner(channel, thread_ts, texts.RESUME_ELSEWHERE.format(link=link))
            return False
        record = state.channel(channel)
        if record is None or record.directory != directory:
            await tell_owner(channel, thread_ts, texts.RESUME_GONE)
            return False
        session = await sessions.resume(channel, thread_ts, chosen.session_id)
        assert session is not None  # just confirmed the channel is bound to `directory`
        # A markdown block, not mrkdwn: the title is escaped so it cannot close or open the bold.
        title = markdown_escape(one_line(chosen.summary, TITLE_LIMIT)) or chosen.session_id
        await say(channel, thread_ts, texts.RESUME_OK.format(title=title))
        return True

    @app.action(RESUME_ACTION)
    async def on_resume(ack: AsyncAck, body: dict[str, Any]) -> None:
        await ack()
        user, team = interaction_actor(body)
        channel = (body.get("channel") or {}).get("id")
        thread_ts = click_thread(body)
        if not await admitted(user, team, channel, thread_ts):
            return
        assert channel is not None
        await reply_on_failure(channel, thread_ts, resume_clicked(channel, body))

    async def resume_clicked(channel: str, body: dict[str, Any]) -> None:
        thread_ts = click_thread(body)
        record = state.channel(channel)
        if record is None:
            await tell_owner(channel, thread_ts, texts.UNBOUND.format(root=config.allowed_root))
            return
        # The button is not trusted: the session must still belong to the channel's directory.
        session_id = str(body["actions"][0].get("value"))
        stored = await sessions.sessions_in(record.directory)
        chosen = next((s for s in stored if s.session_id == session_id), None)
        if chosen is None:
            await tell_owner(channel, thread_ts, texts.RESUME_GONE)
            return
        if await resume_into_new_thread(channel, thread_ts, record.directory, chosen):
            await remove_request(channel, thread_ts, body["message"]["ts"])

    async def remove_request(channel: str, thread_ts: str, ts: str | None) -> None:
        # The tool's line in the reply records the call: the request message has done its job.
        if ts is None:
            return
        await delete_request(slack, channel=channel, ts=ts)
        try:
            # Crash repair (issue #19): spoken for either way, as `ThreadSession._delete_request`
            # does. A no-op for a ts this thread never recorded (a folder or resume picker click).
            # Best-effort (fix round 2 item 1): a failed write here is logged and swallowed.
            state.remove_request(channel, thread_ts, ts)
        except Exception as exc:
            logger.warning(
                "could not clear a deleted request from state.json in %s: %s",
                channel,
                describe(exc),
            )

    async def show_answered(
        channel: str,
        thread_ts: str,
        ts: str | None,
        questions: list[dict[str, Any]],
        answers: dict[str, str | list[str]],
    ) -> None:
        # The terminal keeps what was asked and answered; the request becomes that record.
        if ts is None:
            return
        try:
            # Shares the same process-wide budget every ReplySink draws from: an answered
            # request is rare, but it is still one more chat.update against the same app.
            await sessions.update_limiter.acquire()
            await slack.chat_update(
                channel=channel,
                ts=ts,
                text=texts.ANSWERED,
                blocks=answered_blocks(questions, answers),
            )
        except Exception as exc:
            # The request must not keep buttons that no longer work: remove it, as before.
            logger.warning("could not record an answer in %s: %s", channel, describe(exc))
            await remove_request(channel, thread_ts, ts)
            return
        # Crash repair (issue #19): answered without a delete, so it no longer carries buttons and
        # must still leave the tracked list. Kept outside the try above on purpose (fix round 2
        # item 1): a failed write here must never look like the chat.update itself failed and
        # trigger a delete of a message that was just successfully updated. Best-effort: logged.
        try:
            state.remove_request(channel, thread_ts, ts)
        except Exception as exc:
            logger.warning(
                "could not clear an answered request from state.json in %s: %s",
                channel,
                describe(exc),
            )

    @app.action("question_open")
    async def on_question_open(ack: AsyncAck, body: dict[str, Any]) -> None:
        await ack()
        user, team = interaction_actor(body)
        channel = (body.get("channel") or {}).get("id")
        thread_ts = click_thread(body)
        if not await admitted(user, team, channel, thread_ts):
            return
        assert channel is not None
        approval_id = str(body["actions"][0].get("value"))
        pending = approvals.get(approval_id)
        if (
            pending is None
            or pending.channel_id != channel
            or pending.thread_ts != thread_ts
            or not pending.questions
        ):
            await tell_owner(channel, thread_ts, texts.APPROVAL_GONE)
            return
        # trigger_id lives 3 seconds: the checks above are the only work before this call.
        view = question_view(Draft(approval_id, channel, thread_ts), pending.questions)
        try:
            await slack.views_open(trigger_id=body["trigger_id"], view=view)
        except Exception as exc:  # an expired trigger_id, say: the turn must not wait unseen
            message = texts.QUESTION_NOT_OPENED.format(error=describe(exc))
            await tell_owner(channel, thread_ts, message)

    @app.view(QUESTION_FORM)
    async def on_question_submit(ack: AsyncAck, body: dict[str, Any]) -> None:
        # Slack wants the answer within 3 seconds, and a missing answer can only be shown in
        # it: the checks before it make no network call. The channel is checked before acting.
        user, team = interaction_actor(body)
        view = body.get("view") or {}
        try:
            draft = Draft.load(str(view.get("private_metadata")))
        except (ValueError, KeyError, TypeError):
            await ack()
            return
        if not is_owner(identity, user, team):
            await ack()
            logger.info("ignored an inbound event from someone other than the owner")
            return
        pending = approvals.get(draft.approval_id)
        if (
            pending is None
            or pending.channel_id != draft.channel_id
            or pending.thread_ts != draft.thread_ts
            or not pending.questions
        ):
            await ack()
            await tell_owner(draft.channel_id, draft.thread_ts, texts.APPROVAL_GONE)
            return
        questions = pending.questions
        draft = absorb(draft, (view.get("state") or {}).get("values") or {})
        if not is_answered(draft, questions, draft.active):
            await ack(response_action="errors", errors={f"q{draft.active}": texts.QUESTION_MISSING})
            return
        missing = first_unanswered(draft, questions)
        if missing is not None:
            # Next: the following question, or one left unanswered (never, since each Next
            # checks its own; kept so a draft cannot reach Claude incomplete).
            following = draft.active + 1 if draft.active + 1 < len(questions) else missing
            await ack(
                response_action="update",
                view=question_view(replace(draft, active=following), questions),
            )
            return

        await ack()
        if not await admitted(user, team, draft.channel_id, draft.thread_ts):
            return
        answers = draft_answers(draft, questions)
        assert answers is not None
        resolved = approvals.resolve(
            draft.approval_id, draft.channel_id, draft.thread_ts, Answer(answers)
        )
        if resolved is None:
            await tell_owner(draft.channel_id, draft.thread_ts, texts.APPROVAL_GONE)
            return
        await show_answered(
            draft.channel_id, draft.thread_ts, pending.message_ts, questions, answers
        )

    @app.error
    async def on_error(error: Exception) -> None:
        logger.error("handler failed: %s", type(error).__name__)

    return app
