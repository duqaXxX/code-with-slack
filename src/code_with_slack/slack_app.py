"""The Slack side: every inbound path, each checked on its own before it reaches a session."""

import asyncio
import logging
import re
from collections.abc import Awaitable
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
from code_with_slack.prompt import Prompt
from code_with_slack.render.escape import markdown_escape
from code_with_slack.render.renderer import one_line
from code_with_slack.render.sinks import FALLBACK_LIMIT, describe, split
from code_with_slack.resume import RESUME_ACTION, TITLE_LIMIT, matching, resume_blocks
from code_with_slack.sessions import (
    ChannelSession,
    DirectoryUnavailable,
    SessionManager,
    resolve_directory,
)

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
    guard: ChannelGuard,
    uploads: Path,
    fetch: Fetch | None = None,
) -> AsyncApp:
    async def download_file(*, url: str, mimetype: str, limit: int) -> bytes:
        return await download(url, config.bot_token, mimetype, limit)

    fetch_file = fetch or download_file
    arrival_order: dict[str, asyncio.Lock] = {}

    async def authorize() -> AuthorizeResult:
        # auth.test already ran at startup; who may act is decided by the guards below.
        return AuthorizeResult(
            enterprise_id=None,
            team_id=identity.team_id,
            bot_user_id=identity.bot_user_id,
            bot_token=config.bot_token,
        )

    app = AsyncApp(client=slack, authorize=authorize)

    async def tell_owner(channel: str, text: str) -> None:
        try:
            await slack.chat_postEphemeral(channel=channel, user=identity.owner_user_id, text=text)
        except Exception as exc:
            logger.warning("could not reach the owner in %s: %s", channel, describe(exc))

    async def reply_on_failure(channel: str, work: Awaitable[None]) -> None:
        """A failure after the checks reaches the owner as a line of its own, never as silence."""
        try:
            await work
        except DirectoryUnavailable as exc:
            await tell_owner(channel, exc.message)
        except Exception as exc:
            logger.error("a request failed in %s: %s", channel, type(exc).__name__)
            await tell_owner(channel, texts.ERROR_REPLY.format(error=type(exc).__name__))

    async def admitted(user: str | None, team: str | None, channel: str | None) -> bool:
        if not channel or not is_owner(identity, user, team):
            logger.info("ignored an inbound event from someone other than the owner")
            return False
        reason = await guard.refusal(channel)
        if reason is not None:
            await tell_owner(channel, texts.CHANNEL_REFUSED.format(reason=reason))
            return False
        return True

    async def say(channel: str, text: str) -> None:
        """An answer to one of the daemon's own words, as messages in the channel: a long
        `!help` continues in a new message past a markdown block's limit."""
        for chunk in split(text):
            await slack.chat_postMessage(
                channel=channel,
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
        if not await admitted(user, team, channel):
            return
        assert channel is not None
        await reply_on_failure(channel, handle_message(channel, event))

    async def handle_message(channel: str, event: dict[str, Any]) -> None:
        text = slack_unescape(event.get("text") or "")
        files: list[dict[str, Any]] = event.get("files") or []
        # A message with files is a prompt: no daemon word or command takes a file.
        command = None if files else parse_bang(text)
        if isinstance(command, Help | Guide | Bind):
            await handle_word(channel, command)
            return
        session = sessions.get(channel)
        if session is None:
            await tell_owner(channel, texts.UNBOUND.format(root=config.allowed_root))
            return
        if not (files or command is None or isinstance(command, Passthrough)):
            await handle_word(channel, command)
            return
        # The daemon's words still work while it stops (`!stop` shortens the wait); a new turn
        # would not finish, and Slack does not send this event again to the next instance.
        if sessions.draining:
            await say(channel, texts.RESTARTING)
            return
        # Prompts and commands for Claude Code enter the queue in the order they were sent,
        # although files take a while to download. Every reply goes to the main window.
        directory = session.directory
        async with arrival_order.setdefault(channel, asyncio.Lock()):
            prompt = await with_attachments(channel, text, files) if files else text
            if prompt is None:
                return
            # A !bind or a Resume meanwhile closed the session read above: take the channel's
            # own now, and send nothing if the folder changed under the message.
            current = sessions.get(channel)
            if current is None or current.directory != directory:
                await say(channel, texts.PROMPT_REBOUND)
                return
            if isinstance(command, Passthrough):
                await current.ensure_connected()
                known = {str(c.get("name")) for c in current.commands}
                name = command.text.split(" ", 1)[0]
                prompt = f"/{command.text}" if name in known else text
            await current.submit(prompt)

    async def handle_word(channel: str, command: Word) -> None:
        match command:
            case Help() | Invalid():
                # A mistyped word gets the full list, which shows how each word is written.
                query = command.query if isinstance(command, Help) else ""
                session = sessions.get(channel)
                if session is not None:
                    await session.ensure_connected()
                await say(channel, help_text(session.commands if session else None, query))
            case Guide():
                await say(channel, texts.GUIDE)
            case Bind(path=""):
                await list_folders(channel)
            case Bind(path=path):
                await bind_to(channel, path)
            case _:
                session = sessions.get(channel)
                assert session is not None
                match command:
                    case Bypass(on=on):
                        await session.set_bypass(on)
                        await say(
                            channel,
                            texts.BYPASS_ON
                            if on
                            else texts.BYPASS_OFF.format(mode=session.native_mode),
                        )
                    case Status():
                        await say(channel, await session.status())
                    case Stop():
                        await say(
                            channel,
                            texts.STOPPED if await session.stop() else texts.NOTHING_TO_STOP,
                        )
                    case Resume(target=target):
                        await handle_resume(channel, session, target)

    @app.action("answer")
    async def on_answer(ack: AsyncAck) -> None:
        await ack()  # a menu inside a question: its value is read when Submit is clicked

    async def on_decision(ack: AsyncAck, body: dict[str, Any]) -> None:
        await ack()
        user, team = interaction_actor(body)
        channel = (body.get("channel") or {}).get("id")
        if not await admitted(user, team, channel):
            return
        assert channel is not None
        action = body["actions"][0]
        pending = approvals.get(str(action.get("value")))
        if pending is None or pending.channel_id != channel:
            await tell_owner(channel, texts.APPROVAL_GONE)
            return
        decision: Decision
        match action["action_id"]:
            case "approval_allow":
                decision = Approve()
            case _:
                decision = Deny()
        if approvals.resolve(str(action["value"]), channel, decision) is None:
            await tell_owner(channel, texts.APPROVAL_GONE)
            return
        await remove_request(channel, body["message"]["ts"])

    for action_id in DECISION_ACTIONS:
        app.action(action_id)(on_decision)

    async def with_attachments(
        channel: str, text: str, files: list[dict[str, Any]]
    ) -> Prompt | None:
        """The prompt for `text` and its files; None, after telling the owner why, when any file
        cannot reach Claude: the message is sent whole or not at all."""

        async def refuse(file: dict[str, Any], reason: str) -> None:
            name = markdown_escape(str(file.get("name") or file.get("id")))
            await say(channel, texts.UPLOAD_FAILED.format(name=name, reason=reason))

        for file in files:
            reason = refusal(file)
            if reason is not None:
                await refuse(file, reason)
                return None
        together = images_refusal(files)
        if together is not None:
            await say(channel, together)
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

    async def bind_to(channel: str, path: str) -> None:
        directory = await folder_named(channel, path)
        if directory is not None:
            await sessions.bind(channel, directory)
            await say(channel, texts.BIND_OK.format(directory=directory))

    async def folder_named(channel: str, path: str) -> Path | None:
        directory = resolve_directory(path, config.allowed_root)
        if directory is None:
            await say(channel, texts.BIND_OUTSIDE.format(path=path, root=config.allowed_root))
        return directory

    async def list_folders(channel: str) -> None:
        root = config.allowed_root
        stored = sessions.get(channel)
        folders = await sessions.folders_in(root)
        blocks = bind_blocks(root, folders, stored.directory if stored else None)
        await slack.chat_postMessage(
            channel=channel,
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
        if not await admitted(user, team, channel):
            return
        assert channel is not None
        await reply_on_failure(channel, bind_clicked(channel, body))

    async def bind_clicked(channel: str, body: dict[str, Any]) -> None:
        # The button is not trusted: its folder goes through the same check as a typed `!bind`.
        directory = await folder_named(channel, str(body["actions"][0].get("value")))
        if directory is None:
            return
        current = sessions.get(channel)
        if current is not None and current.directory == directory:
            await say(channel, texts.BIND_ALREADY.format(directory=directory))
            return
        # A list can be old: unlike a typed `!bind`, a click never ends work in flight.
        if not await sessions.bind_when_idle(channel, directory):
            await say(channel, texts.BIND_BUSY)
            return
        await say(channel, texts.BIND_OK.format(directory=directory))
        await remove_request(channel, body["message"]["ts"])

    async def handle_resume(channel: str, session: ChannelSession, target: str) -> None:
        # Only the list shows dates: matching a target needs none, and dating reads every file.
        stored = await sessions.sessions_in(session.directory, dated=not target)
        if not target:
            current = sessions.current_session(channel)
            blocks = resume_blocks(session.directory, stored, current, datetime.now().astimezone())
            await slack.chat_postMessage(
                channel=channel,
                text=texts.RESUME_LIST.format(directory=session.directory),
                blocks=blocks,
                unfurl_links=False,
                unfurl_media=False,
            )
            return
        found = matching(stored, target)
        if len(found) != 1:
            template = texts.RESUME_AMBIGUOUS if found else texts.RESUME_NONE
            await say(channel, template.format(directory=session.directory, target=target))
            return
        await resume_session(channel, found[0], session.directory)

    async def resume_session(channel: str, chosen: SDKSessionInfo, directory: Path) -> bool:
        """Point the channel at `chosen`, a session read from `directory`; False when nothing
        changed and the owner was told why."""
        current = sessions.get(channel)
        # The listing awaited: a `!bind` meanwhile moved the channel, and `chosen` belongs to
        # the old folder. No await from this check to the store in `sessions.resume`.
        if current is None or current.directory != directory:
            await tell_owner(channel, texts.RESUME_GONE)
            return False
        if chosen.session_id == sessions.current_session(channel):
            # Resuming it again would close the live client and turn bypass off for nothing.
            await say(channel, texts.RESUME_ALREADY)
            return False
        if not await sessions.resume(channel, chosen.session_id):
            await say(channel, texts.RESUME_BUSY)
            return False
        # A markdown block, not mrkdwn: the title is escaped so it cannot close or open the bold.
        title = markdown_escape(one_line(chosen.summary, TITLE_LIMIT)) or chosen.session_id
        await say(channel, texts.RESUME_OK.format(title=title))
        return True

    @app.action(RESUME_ACTION)
    async def on_resume(ack: AsyncAck, body: dict[str, Any]) -> None:
        await ack()
        user, team = interaction_actor(body)
        channel = (body.get("channel") or {}).get("id")
        if not await admitted(user, team, channel):
            return
        assert channel is not None
        await reply_on_failure(channel, resume_clicked(channel, body))

    async def resume_clicked(channel: str, body: dict[str, Any]) -> None:
        session = sessions.get(channel)
        if session is None:
            await tell_owner(channel, texts.UNBOUND.format(root=config.allowed_root))
            return
        # The button is not trusted: the session must still belong to the channel's directory.
        session_id = str(body["actions"][0].get("value"))
        stored = await sessions.sessions_in(session.directory)
        chosen = next((s for s in stored if s.session_id == session_id), None)
        if chosen is None:
            await tell_owner(channel, texts.RESUME_GONE)
            return
        if await resume_session(channel, chosen, session.directory):
            await remove_request(channel, body["message"]["ts"])

    async def remove_request(channel: str, ts: str | None) -> None:
        # The tool's line in the reply records the call: the request message has done its job.
        if ts is None:
            return
        try:
            await slack.chat_delete(channel=channel, ts=ts)
        except Exception as exc:
            logger.warning("could not remove a request in %s: %s", channel, describe(exc))

    @app.action("question_open")
    async def on_question_open(ack: AsyncAck, body: dict[str, Any]) -> None:
        await ack()
        user, team = interaction_actor(body)
        channel = (body.get("channel") or {}).get("id")
        if not await admitted(user, team, channel):
            return
        assert channel is not None
        approval_id = str(body["actions"][0].get("value"))
        pending = approvals.get(approval_id)
        if pending is None or pending.channel_id != channel or not pending.questions:
            await tell_owner(channel, texts.APPROVAL_GONE)
            return
        # trigger_id lives 3 seconds: the checks above are the only work before this call.
        view = question_view(Draft(approval_id, channel), pending.questions)
        try:
            await slack.views_open(trigger_id=body["trigger_id"], view=view)
        except Exception as exc:  # an expired trigger_id, say: the turn must not wait unseen
            await tell_owner(channel, texts.QUESTION_NOT_OPENED.format(error=describe(exc)))

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
        if pending is None or pending.channel_id != draft.channel_id or not pending.questions:
            await ack()
            await tell_owner(draft.channel_id, texts.APPROVAL_GONE)
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
        if not await admitted(user, team, draft.channel_id):
            return
        answers = draft_answers(draft, questions)
        assert answers is not None
        if approvals.resolve(draft.approval_id, draft.channel_id, Answer(answers)) is None:
            await tell_owner(draft.channel_id, texts.APPROVAL_GONE)
            return
        await remove_request(draft.channel_id, pending.message_ts)

    @app.error
    async def on_error(error: Exception) -> None:
        logger.error("handler failed: %s", type(error).__name__)

    return app
