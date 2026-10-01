"""Forgetting what `state.json` no longer needs: a bound channel Slack no longer has, and a
thread whose session is gone. Run once on start and then every `CLEAN_EVERY_SECONDS`, so a daemon
that stays up for weeks cleans too.

Only a certain answer removes anything. For a channel that is `channel_not_found` from
`conversations.info` (its reference, read 2026-10-01; measured the same day on deleted channels),
which is also what a private channel the bot was removed from answers: the two cannot be told
apart, and a channel forgotten by mistake is bound again with `!bind`. A rate limit, a server
error or the network is no answer and removes nothing. For a thread the rule is
`StateStore.prune`'s.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Collection
from pathlib import Path

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack.render.sinks import describe
from code_with_slack.state import StateStore

logger = logging.getLogger(__name__)

CLEAN_EVERY_SECONDS = 6 * 60 * 60
CHANNEL_NOT_FOUND = "channel_not_found"

# The (channel_id, thread_ts) of every thread with a live session in the daemon, read at the
# moment it is needed: nothing of theirs is removed under them.
Live = Callable[[], Collection[tuple[str, str]]]


async def forget_gone_channels(slack: AsyncWebClient, state: StateStore, live: Live) -> list[str]:
    """Remove from `state` each bound channel Slack answers `channel_not_found` about, with its
    threads, and return their ids. A channel with a live session is left for the next pass. When
    Slack finds none of the bound channels (a channel it gave no answer about is not one it
    found), nothing is removed: that is what the token of another workspace, or an app removed
    from every channel, looks like, and forgetting every binding would be the wrong repair."""
    bound = state.channels()
    gone = []
    found = 0
    for channel_id in bound:
        try:
            await slack.conversations_info(channel=channel_id)
            found += 1
        except SlackApiError as exc:
            if describe(exc) == CHANNEL_NOT_FOUND:
                gone.append(channel_id)
            else:
                logger.warning("could not read channel %s: %s", channel_id, describe(exc))
        except Exception as exc:
            logger.warning("could not read channel %s: %s", channel_id, describe(exc))
    if not gone:
        return []
    if not found:
        logger.warning(
            "Slack finds none of the %d bound channels: nothing is forgotten (check the "
            "workspace of the bot token, and that the bot is still in its channels)",
            len(bound),
        )
        return []
    busy = {channel_id for channel_id, _ in live()}
    forgotten = []
    for channel_id in gone:
        record = state.channel(channel_id)
        if record is None or channel_id in busy:
            continue
        state.remove_channel(channel_id)
        forgotten.append(channel_id)
        logger.info(
            "forgot channel %s: Slack no longer has it (%d thread(s))",
            channel_id,
            len(record.threads),
        )
    return forgotten


async def clean(
    slack: AsyncWebClient,
    state: StateStore,
    *,
    alive: Callable[[Path], Collection[str] | None],
    live: Live,
) -> None:
    """One pass: forget the channels that are gone, then prune the threads whose session is
    (`alive` gives a folder's session ids, or None when it cannot tell). Never raises: a pass
    that fails is logged and the next one tries again."""
    try:
        await forget_gone_channels(slack, state, live)
    except Exception as exc:
        logger.warning("could not check the bound channels: %s", describe(exc))
    try:
        folders = {t.directory for _, _, t in state.threads() if t.session_id is not None}
        # Reading a folder's sessions is blocking file work: done off the event loop, and the
        # state is touched only once back on it.
        known = await asyncio.to_thread(lambda: {folder: alive(folder) for folder in folders})
        removed = state.prune(known.get, time.time(), keep=live())
        if removed:
            logger.info("pruned %d stale thread(s) from state.json", removed)
    except Exception as exc:
        logger.warning("could not prune stale threads: %s", exc)
