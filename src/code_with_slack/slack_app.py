"""The Slack side: every inbound path, each checked on its own before it reaches a session."""

import logging
from typing import Any

from slack_bolt.async_app import AsyncAck, AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.approvals import (
    Answer,
    Approvals,
    Approve,
    Decision,
    Deny,
    outcome_blocks,
    read_answers,
)
from code_with_slack.commands import (
    Bind,
    Bypass,
    Invalid,
    Passthrough,
    Picker,
    Status,
    Stop,
    bang_command,
    parse_cc,
)
from code_with_slack.config import Config
from code_with_slack.guards import (
    ChannelGuard,
    Identity,
    command_actor,
    interaction_actor,
    is_owner,
    is_prompt_message,
    message_actor,
)
from code_with_slack.render.renderer import one_line
from code_with_slack.sessions import SessionManager, resolve_directory

logger = logging.getLogger(__name__)
MAX_OPTIONS = 100
OUTCOMES = {
    "approval_allow": texts.APPROVED,
    "approval_deny": texts.DENIED,
    "question_submit": texts.ANSWERED,
    "question_skip": texts.SKIPPED,
}


def slack_unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def picker_blocks() -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": texts.PICKER_PROMPT},
            "accessory": {
                "type": "external_select",
                "action_id": "picker_select",
                "min_query_length": 0,
                "placeholder": {"type": "plain_text", "text": texts.PICKER_PLACEHOLDER},
            },
        }
    ]


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
        except SlackApiError as exc:
            logger.warning(
                "could not reach the owner in %s: %s", channel, exc.response.get("error")
            )

    async def admitted(user: str | None, team: str | None, channel: str | None) -> bool:
        if not channel or not is_owner(identity, user, team):
            logger.info("ignored an inbound event from someone other than the owner")
            return False
        reason = await guard.refusal(channel)
        if reason is not None:
            await tell_owner(channel, texts.CHANNEL_REFUSED.format(reason=reason))
            return False
        return True

    async def run_command(channel: str, text: str) -> None:
        session = sessions.get(channel)
        if session is None:
            await tell_owner(channel, texts.UNBOUND)
            return
        root = await slack.chat_postMessage(
            channel=channel, text=texts.COMMAND_ROOT.format(command=text)
        )
        await session.submit(f"/{text}", str(root["ts"]))

    @app.event("message")
    async def on_message(event: dict[str, Any]) -> None:
        if not is_prompt_message(event):
            return
        user, team = message_actor(event)
        channel = event.get("channel")
        if not await admitted(user, team, channel):
            return
        assert channel is not None
        session = sessions.get(channel)
        if session is None:
            await tell_owner(channel, texts.UNBOUND)
            return
        text = slack_unescape(event["text"])
        prompt = text
        if text.lstrip().startswith("!"):
            await session.ensure_connected()
            command = bang_command(text, {str(c.get("name")) for c in session.commands})
            prompt = f"/{command}" if command else text
        await session.submit(prompt, str(event.get("thread_ts") or event["ts"]))

    @app.command("/cc")
    async def on_cc(ack: AsyncAck, body: dict[str, Any]) -> None:
        await ack()
        user, team = command_actor(body)
        channel = body.get("channel_id")
        if not await admitted(user, team, channel):
            return
        assert channel is not None
        match parse_cc(body.get("text") or ""):
            case Bind(path=path):
                directory = resolve_directory(path, config.allowed_root)
                if directory is None:
                    await tell_owner(
                        channel, texts.BIND_OUTSIDE.format(path=path, root=config.allowed_root)
                    )
                else:
                    await sessions.bind(channel, directory)
                    await tell_owner(channel, texts.BIND_OK.format(directory=directory))
            case Invalid():
                await tell_owner(channel, texts.USAGE)
            case Passthrough(text=text):
                await run_command(channel, text)
            case parsed:
                session = sessions.get(channel)
                if session is None:
                    await tell_owner(channel, texts.UNBOUND)
                    return
                match parsed:
                    case Bypass(on=on):
                        await session.set_bypass(on)
                        await tell_owner(
                            channel,
                            texts.BYPASS_ON
                            if on
                            else texts.BYPASS_OFF.format(mode=session.native_mode),
                        )
                    case Status():
                        await tell_owner(channel, session.status())
                    case Stop():
                        await tell_owner(
                            channel,
                            texts.STOPPED if await session.stop() else texts.NOTHING_TO_STOP,
                        )
                    case Picker():
                        await session.ensure_connected()
                        await slack.chat_postEphemeral(
                            channel=channel,
                            user=identity.owner_user_id,
                            text=texts.PICKER_PROMPT,
                            blocks=picker_blocks(),
                        )

    @app.options("picker_select")
    async def on_picker_options(ack: AsyncAck, body: dict[str, Any]) -> None:
        user, team = interaction_actor(body)
        channel = (body.get("channel") or {}).get("id")
        session = sessions.get(channel) if channel and is_owner(identity, user, team) else None
        if session is None:
            await ack(options=[])
            return
        query = str(body.get("value") or "").lower()
        matches = [c for c in session.commands if query in str(c.get("name", "")).lower()][
            :MAX_OPTIONS
        ]
        await ack(
            options=[
                {
                    "text": {"type": "plain_text", "text": one_line(f"/{c['name']}", 75)},
                    "value": str(c["name"])[:150],
                }
                for c in matches
            ]
        )

    @app.action("picker_select")
    async def on_picker(ack: AsyncAck, body: dict[str, Any]) -> None:
        await ack()
        user, team = interaction_actor(body)
        channel = (body.get("channel") or {}).get("id")
        if not await admitted(user, team, channel):
            return
        assert channel is not None
        await run_command(channel, str(body["actions"][0]["selected_option"]["value"]))

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
        outcome = OUTCOMES[action["action_id"]].format(title=pending.title)
        await slack.chat_update(
            channel=channel, ts=body["message"]["ts"], text=outcome, blocks=outcome_blocks(outcome)
        )

    for action_id in OUTCOMES:
        app.action(action_id)(on_decision)

    @app.error
    async def on_error(error: Exception) -> None:
        logger.error("handler failed: %s", type(error).__name__)

    return app
