"""`code-with-slack`: start the daemon (normally from the LaunchAgent in docs/setup.md)."""

import asyncio
import contextlib
import logging
import signal
import sys
import time
from collections.abc import Collection
from pathlib import Path

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.approvals import Approvals
from code_with_slack.attachments import prepare_uploads, uploads_dir
from code_with_slack.config import CONFIG_DIR, ConfigError, load_config
from code_with_slack.footer import UsageCache, UsageProbe
from code_with_slack.guards import ChannelGuard, Identity
from code_with_slack.lock import AlreadyRunning, single_instance
from code_with_slack.render.sinks import context_block, describe, notice_text
from code_with_slack.sessions import (
    SessionDeps,
    SessionManager,
    default_client_factory,
    directory_sessions,
)
from code_with_slack.slack_app import build_app
from code_with_slack.state import StateError, StateStore

logger = logging.getLogger("code_with_slack")

# How long a stop waits for running turns before it ends them itself. It holds after
# `launchctl kill TERM`, which only sends the signal; a stop launchd makes itself (`bootout`,
# `kickstart -k`) kills the process after ExitTimeOut, which launchd caps at 60 seconds.
DRAIN_LIMIT_SECONDS = 1740


def _alive_sessions(directory: Path) -> Collection[str]:
    """The session ids `directory_sessions` still finds in `directory`, for `state.prune`."""
    return {info.session_id for info in directory_sessions(directory)}


async def _post_upgrade_notices(slack: AsyncWebClient, state: StateStore) -> None:
    """The v1-to-v2 migration notice (D7), once per channel, top-level: not a reply to any
    message, so it carries no thread_ts."""
    for channel_id in state.pending_notices():
        try:
            await slack.chat_postMessage(
                channel=channel_id,
                text=texts.UPGRADE_NOTICE,
                blocks=[context_block(notice_text(texts.UPGRADE_NOTICE))],
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception as exc:
            logger.warning("could not post the upgrade notice in %s: %s", channel_id, describe(exc))
            continue
        state.clear_notice(channel_id)


async def run(config_dir: Path = CONFIG_DIR) -> None:
    config = load_config(config_dir)
    with single_instance(config_dir):
        state = StateStore(config_dir / "state.json")
        try:
            removed = state.prune(_alive_sessions, time.time())
            if removed:
                logger.info("pruned %d stale thread(s) from state.json", removed)
        except Exception as exc:
            logger.warning("could not prune stale threads: %s", exc)
        uploads = uploads_dir()
        prepare_uploads(uploads)
        slack = AsyncWebClient(token=config.bot_token)
        slack.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=3))
        auth = await slack.auth_test()
        identity = Identity(config.owner_user_id, str(auth["team_id"]), str(auth["user_id"]))
        probe = UsageProbe(Path.home(), default_client_factory)
        approvals = Approvals()
        sessions = SessionManager(
            SessionDeps(
                slack=slack,
                identity=identity,
                state=state,
                approvals=approvals,
                usage=UsageCache(probe),
                client_factory=default_client_factory,
            )
        )
        app = build_app(
            slack=slack,
            config=config,
            identity=identity,
            sessions=sessions,
            approvals=approvals,
            guard=ChannelGuard(slack, identity),
            state=state,
            uploads=uploads,
        )
        handler = AsyncSocketModeHandler(app, config.app_token)

        stop = asyncio.Event()
        received: list[signal.Signals] = []

        def on_signal(sig: signal.Signals) -> None:
            received.append(sig)
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, on_signal, sig)
        await handler.connect_async()  # type: ignore[no-untyped-call]  # untyped in Bolt 1.30.0
        logger.info("connected to Slack workspace %s", identity.team_id)
        try:
            await _post_upgrade_notices(slack, state)
            await stop.wait()
            # launchd stops and restarts with SIGTERM: the turns already running finish first. Not
            # on SIGINT: a Ctrl-C in a terminal reaches the Claude Code processes too, which share
            # the daemon's process group. A second signal stops without waiting.
            if received[0] == signal.SIGTERM:
                stop.clear()
                logger.info("stopping: letting running turns finish")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(sessions.drain(stop), DRAIN_LIMIT_SECONDS)
        finally:
            logger.info("shutting down")
            await handler.close_async()  # type: ignore[no-untyped-call]
            await sessions.close_all()
            await probe.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(CONFIG_DIR))
    except (ConfigError, StateError, AlreadyRunning) as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
