"""Who may talk to the daemon, and in which channels it may answer.

Every inbound path calls these on its own: a button is never trusted because of the message it
sits on, and the channel is re-read from Slack each time, since membership can change.
"""

from dataclasses import dataclass
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts


@dataclass(frozen=True)
class Identity:
    owner_user_id: str
    team_id: str
    bot_user_id: str


def is_owner(identity: Identity, user_id: str | None, team_id: str | None) -> bool:
    """True only for the configured owner acting from the workspace read at startup."""
    return bool(user_id) and user_id == identity.owner_user_id and team_id == identity.team_id


def message_actor(event: dict[str, Any]) -> tuple[str | None, str | None]:
    return event.get("user"), event.get("team")


def command_actor(body: dict[str, Any]) -> tuple[str | None, str | None]:
    return body.get("user_id"), body.get("team_id")


def interaction_actor(body: dict[str, Any]) -> tuple[str | None, str | None]:
    """The user and the workspace of a click; a user whose home team differs counts as foreign."""
    user = body.get("user") or {}
    team = (body.get("team") or {}).get("id")
    home = user.get("team_id")
    return user.get("id"), team if home in (None, team) else None


def is_prompt_message(event: dict[str, Any]) -> bool:
    """A plain message a person typed: no subtype (edits, deletes, joins), no bot, some text."""
    return (
        event.get("type") == "message"
        and "subtype" not in event
        and "bot_id" not in event
        and bool((event.get("text") or "").strip())
    )


class ChannelGuard:
    def __init__(self, slack: AsyncWebClient, identity: Identity) -> None:
        self._slack = slack
        self._identity = identity

    async def refusal(self, channel_id: str) -> str | None:
        """None when the channel is private, unshared and holds exactly the owner and the bot."""
        try:
            info = (await self._slack.conversations_info(channel=channel_id))["channel"]
            members = await self._slack.conversations_members(channel=channel_id, limit=10)
        except SlackApiError:
            return texts.REASON_UNREADABLE
        if not info.get("is_private") or info.get("is_im") or info.get("is_mpim"):
            return texts.REASON_NOT_PRIVATE
        if any(
            info.get(flag)
            for flag in ("is_shared", "is_ext_shared", "is_org_shared", "is_pending_ext_shared")
        ):
            return texts.REASON_SHARED
        more = (members.get("response_metadata") or {}).get("next_cursor")
        expected = {self._identity.owner_user_id, self._identity.bot_user_id}
        if more or set(members["members"]) != expected:
            return texts.REASON_MEMBERS
        return None
