"""Deleting from Slack what the Home tab's edit mode names: a session's whole thread, or what
sits in a channel outside every thread (the commands typed there and the bot's own messages).

`chat.delete` with a bot token "may delete only messages posted by that bot", and with a user
token the messages that user can delete (docs.slack.dev/reference/methods/chat.delete, read
2026-10-05). A session's thread is started by the owner, so its root and the owner's replies
need the owner's own user token (`SLACK_USER_TOKEN`, user scope `chat:write`): the bot's
messages are deleted with the bot token, every other one with the owner's. Without that token
there is no deleter, and the page offers no delete.

The replies go first and the root last: the reference does not say what a root deleted under its
replies leaves behind. `chat.delete` is Tier 3 (50+ per minute); both clients retry a rate limit
after the time Slack asks for, which is what a delete's time is made of (measured 2026-10-05
on a free workspace: 20 to 40 seconds a thread, with a rate limit met every few calls), so
threads are deleted one at a time. A delete that stops half way leaves the thread in `state.json`,
so asking again continues with the messages that are left. The Claude Code session is not
touched: its transcript stays, and `!resume` lists it again once no thread holds it.

Cleaning up a channel reads its history (`conversations.history`, Tier 3, cursor pagination,
read 2026-10-05) and deletes the messages that are no thread and belong to no thread: "Detect a
threaded message by looking for a `thread_ts` value in the message object", and a parent keeps
it "even if all its replies have been deleted" (docs.slack.dev/messaging/retrieving-messages,
read 2026-10-05). In a channel bound to a folder every message the owner sends outside a thread
is a prompt or a word for the daemon, so one with no reply is a leftover whoever wrote it: the
owner's and the bot's both go. What stays: a thread that has a reply, a message with a
`subtype` (Slack's own lines), anyone else's message, and a message `state.json` holds as a
thread whose first reply has not come yet. A message that carries `thread_ts` is deleted only
after `conversations.replies` on it, with `limit=1`, returned its root with no `reply_count`
(a root nobody replied to has none, measured 2026-10-01): the reference does not say what a
parent's count reads in the channel's history once its replies are gone, and a thread taken for
a leftover would lose its root.
"""

import asyncio
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
        owner_user_id: str,
        state: StateStore,
        release: Callable[[str, str], Awaitable[bool]],
    ) -> None:
        self._bot = bot
        self._owner = owner
        self._bot_user_id = bot_user_id
        self._owner_user_id = owner_user_id
        self._state = state
        self._release = release
        # One thread at a time: two deletes at once would share Slack's rate limit, and each
        # would use up its retries on the other's calls.
        self._one_at_a_time = asyncio.Lock()

    async def delete(self, channel_id: str, thread_ts: str) -> str | None:
        """Delete every message of a thread that holds a session and forget the thread. Returns
        None once the thread is gone (or was not one of the daemon's), else the line that says
        why it is still there. Never raises."""
        if self._state.thread(channel_id, thread_ts) is None:
            return None  # not a thread of the daemon's: nothing a click may delete
        async with self._one_at_a_time:
            return await self._delete(channel_id, thread_ts)

    async def _delete(self, channel_id: str, thread_ts: str) -> str | None:
        if self._state.thread(channel_id, thread_ts) is None:
            return None  # deleted while this call waited its turn
        try:
            if not await self._release(channel_id, thread_ts):
                return texts.HOME_DELETE_BUSY
            messages = await self._messages(channel_id, thread_ts)
            # The root last, so a delete that stops half way leaves a thread, not loose replies.
            for ts, author in sorted(messages.items(), key=lambda item: item[0] == thread_ts):
                await self._remove(channel_id, ts, author)
        except Exception as exc:
            logger.warning("could not delete %s/%s: %s", channel_id, thread_ts, describe(exc))
            return texts.HOME_DELETE_FAILED.format(error=describe(exc))
        self._state.remove_thread(channel_id, thread_ts)
        logger.info("deleted thread %s/%s", channel_id, thread_ts)
        return None

    async def _remove(self, channel_id: str, ts: str, author: str) -> None:
        client = self._bot if author == self._bot_user_id else self._owner
        try:
            await client.chat_delete(channel=channel_id, ts=ts)
        except SlackApiError as exc:
            if describe(exc) != MESSAGE_NOT_FOUND:  # already gone is what was asked
                raise

    async def clean(self, channel_id: str) -> str | None:
        """Delete what sits in a bound channel outside every thread: the owner's messages and
        the bot's that have no reply. Returns None when done (or for a channel that
        is not bound), else the line that says why it stopped. Never raises."""
        if channel_id not in self._state.channels():
            return None  # not a channel of the daemon's: nothing a click may clean
        async with self._one_at_a_time:
            try:
                loose = await self._loose(channel_id)
                for ts, author in loose.items():
                    await self._remove(channel_id, ts, author)
            except Exception as exc:
                logger.warning("could not clean up %s: %s", channel_id, describe(exc))
                return texts.HOME_CLEAN_FAILED.format(error=describe(exc))
        logger.info("cleaned up %s: %d messages", channel_id, len(loose))
        return None

    async def _loose(self, channel_id: str) -> dict[str, str]:
        """The channel's messages a clean-up deletes, `ts` to author, read page by page."""
        found: dict[str, str] = {}
        cursor: str | None = None
        while True:
            extra: dict[str, Any] = {"cursor": cursor} if cursor else {}
            page = await self._bot.conversations_history(channel=channel_id, limit=PAGE, **extra)
            for message in page.get("messages") or []:
                ts, author = str(message["ts"]), str(message.get("user") or "")
                threaded = message.get("thread_ts")
                if message.get("subtype") or (threaded and str(threaded) != ts):
                    continue  # Slack's own line; or a thread's reply shown in the channel
                if author not in (self._bot_user_id, self._owner_user_id):
                    continue
                if self._state.thread(channel_id, ts) is not None:
                    continue  # a thread of the daemon's with no reply yet
                if threaded and await self._has_replies(channel_id, ts):
                    continue  # a thread
                found[ts] = author
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return found

    async def _has_replies(self, channel_id: str, ts: str) -> bool:
        """Whether a message that carries `thread_ts` still has a reply, asked of the thread
        itself. True as well for one that is gone meanwhile: there is nothing to delete."""
        try:
            answer = await self._bot.conversations_replies(channel=channel_id, ts=ts, limit=1)
        except SlackApiError as exc:
            if describe(exc) == THREAD_NOT_FOUND:
                return True
            raise
        messages = answer.get("messages") or []
        root = next((m for m in messages if str(m.get("ts")) == ts), None)
        # No root in the answer is no proof of an empty thread: keep the message.
        return root is None or bool(root.get("reply_count")) or len(messages) > 1

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
