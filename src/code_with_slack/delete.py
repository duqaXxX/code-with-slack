"""Deleting a session's whole thread from Slack, asked for from the Home tab's edit mode.

`chat.delete` with a bot token "may delete only messages posted by that bot", and with a user
token the messages that user can delete (docs.slack.dev/reference/methods/chat.delete, read
2026-10-05). A session's thread is started by the owner, so its root and the owner's replies
need the owner's own user token (`SLACK_USER_TOKEN`, user scope `chat:write`): the bot's
messages are deleted with the bot token, every other one with the owner's. Without that token
there is no deleter, and the page offers no delete.

The replies go first and the root last: the reference does not say what a root deleted under its
replies leaves behind. `chat.delete` is Tier 3 (50+ per minute); both clients retry a rate limit
after the time Slack asks for. A delete that stops half way leaves the thread in `state.json`,
so asking again continues with the messages that are left. The Claude Code session is not
touched: its transcript stays, and `!resume` lists it again once no thread holds it.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.render.sinks import describe
from code_with_slack.state import StateStore

logger = logging.getLogger(__name__)

# How many messages one `conversations.replies` page is asked for.
PAGE = 200
MESSAGE_NOT_FOUND = "message_not_found"
THREAD_NOT_FOUND = "thread_not_found"


class ThreadDeleter:
    """Deletes the threads the Home tab's Delete names. `release` closes the thread's live
    session when it is idle and says whether the thread is free (`SessionManager.release`)."""

    def __init__(
        self,
        bot: AsyncWebClient,
        owner: AsyncWebClient,
        *,
        bot_user_id: str,
        state: StateStore,
        release: Callable[[str, str], Awaitable[bool]],
    ) -> None:
        self._bot = bot
        self._owner = owner
        self._bot_user_id = bot_user_id
        self._state = state
        self._release = release

    async def delete(self, channel_id: str, thread_ts: str) -> str | None:
        """Delete every message of a thread that holds a session and forget the thread. Returns
        None once the thread is gone (or was not one of the daemon's), else the line that says
        why it is still there. Never raises."""
        if self._state.thread(channel_id, thread_ts) is None:
            return None  # not a thread of the daemon's: nothing a click may delete
        try:
            if not await self._release(channel_id, thread_ts):
                return texts.HOME_DELETE_BUSY
            messages = await self._messages(channel_id, thread_ts)
            # The root last, so a delete that stops half way leaves a thread, not loose replies.
            for ts, author in sorted(messages.items(), key=lambda item: item[0] == thread_ts):
                client = self._bot if author == self._bot_user_id else self._owner
                try:
                    await client.chat_delete(channel=channel_id, ts=ts)
                except SlackApiError as exc:
                    if describe(exc) != MESSAGE_NOT_FOUND:  # already gone is what was asked
                        raise
        except Exception as exc:
            logger.warning("could not delete %s/%s: %s", channel_id, thread_ts, describe(exc))
            return texts.HOME_DELETE_FAILED.format(error=describe(exc))
        self._state.remove_thread(channel_id, thread_ts)
        logger.info("deleted thread %s/%s", channel_id, thread_ts)
        return None

    async def _messages(self, channel_id: str, thread_ts: str) -> dict[str, str]:
        """Every message of the thread, its `ts` to its author's user id, read page by page
        (cursor pagination, `response_metadata.next_cursor`). Empty when the root is gone."""
        found: dict[str, str] = {}
        cursor: str | None = None
        while True:
            extra: dict[str, Any] = {"cursor": cursor} if cursor else {}
            try:
                page = await self._bot.conversations_replies(
                    channel=channel_id, ts=thread_ts, limit=PAGE, **extra
                )
            except SlackApiError as exc:
                if describe(exc) == THREAD_NOT_FOUND:
                    return found
                raise
            for message in page.get("messages") or []:
                found[str(message["ts"])] = str(message.get("user") or "")
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return found
