"""The Slack side: every inbound path, each checked on its own before it reaches a session."""

import logging
from collections.abc import Awaitable
from typing import Any

from slack_bolt.async_app import AsyncAck, AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.approvals import (
    Answer,
    Approvals,
    Approve,
    Decision,
    Deny,
    read_answers,
)
from code_with_slack.commands import (
    Bind,
    Bypass,
    Command,
    Help,
    Invalid,
    Passthrough,
    Status,
    Stop,
    help_text,
    parse_bang,
)
from code_with_slack.config import Config
from code_with_slack.guards import (
    ChannelGuard,
    Identity,
    interaction_actor,
    is_owner,
    is_prompt_message,
    message_actor,
)
from code_with_slack.render.sinks import FALLBACK_LIMIT, describe, split
from code_with_slack.sessions import DirectoryUnavailable, SessionManager, resolve_directory

logger = logging.getLogger(__name__)
DECISION_ACTIONS = ("approval_allow", "approval_deny", "question_submit", "question_skip")


def slack_unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def build_app(
    *,
    slack: AsyncWebClient,
    config: Config,
    identity: Identity,
    sessions: SessionManager,
    approvals: Approvals,
    guard: ChannelGuard,
) -> AsyncApp:
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
        text = slack_unescape(event["text"])
        command = parse_bang(text)
        if isinstance(command, Help | Bind):
            await handle_word(channel, command)
            return
        session = sessions.get(channel)
        if session is None:
            await tell_owner(channel, texts.UNBOUND)
            return
        if command is None:
            # Every reply goes to the main window, even for a message written inside a thread.
            await session.submit(text)
        elif isinstance(command, Passthrough):
            await session.ensure_connected()
            known = {str(c.get("name")) for c in session.commands}
            name = command.text.split(" ", 1)[0]
            await session.submit(f"/{command.text}" if name in known else text)
        else:
            await handle_word(channel, command)

    async def handle_word(channel: str, command: Command) -> None:
        match command:
            case Help() | Invalid():
                # A mistyped word gets the full list, which shows how each word is written.
                session = sessions.get(channel)
                if session is not None:
                    await session.ensure_connected()
                await say(channel, help_text(session.commands if session else None))
            case Bind(path=path):
                directory = resolve_directory(path, config.allowed_root)
                if directory is None:
                    await say(
                        channel, texts.BIND_OUTSIDE.format(path=path, root=config.allowed_root)
                    )
                else:
                    await sessions.bind(channel, directory)
                    await say(channel, texts.BIND_OK.format(directory=directory))
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
                        await say(channel, session.status())
                    case Stop():
                        await say(
                            channel,
                            texts.STOPPED if await session.stop() else texts.NOTHING_TO_STOP,
                        )

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
            case "question_submit":
                answers = read_answers(
                    pending.questions or [], (body.get("state") or {}).get("values") or {}
                )
                if answers is None:
                    await tell_owner(channel, texts.QUESTION_INCOMPLETE)
                    return
                decision = Answer(answers)
            case _:
                decision = Deny()
        if approvals.resolve(str(action["value"]), channel, decision) is None:
            await tell_owner(channel, texts.APPROVAL_GONE)
            return
        # The tool's line in the reply records the call: the request message has done its job.
        try:
            await slack.chat_delete(channel=channel, ts=body["message"]["ts"])
        except Exception as exc:
            logger.warning("could not remove a request in %s: %s", channel, describe(exc))

    for action_id in DECISION_ACTIONS:
        app.action(action_id)(on_decision)

    @app.error
    async def on_error(error: Exception) -> None:
        logger.error("handler failed: %s", type(error).__name__)

    return app
