"""The Slack side: every inbound path, each checked on its own before it reaches a session."""

import logging
import re
from collections.abc import Awaitable
from dataclasses import replace
from typing import Any

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
from code_with_slack.commands import (
    Bind,
    Bypass,
    Help,
    Invalid,
    Passthrough,
    Status,
    Stop,
    Word,
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

    async def handle_word(channel: str, command: Word) -> None:
        match command:
            case Help() | Invalid():
                # A mistyped word gets the full list, which shows how each word is written.
                query = command.query if isinstance(command, Help) else ""
                session = sessions.get(channel)
                if session is not None:
                    await session.ensure_connected()
                await say(channel, help_text(session.commands if session else None, query))
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
            case _:
                decision = Deny()
        if approvals.resolve(str(action["value"]), channel, decision) is None:
            await tell_owner(channel, texts.APPROVAL_GONE)
            return
        await remove_request(channel, body["message"]["ts"])

    for action_id in DECISION_ACTIONS:
        app.action(action_id)(on_decision)

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
