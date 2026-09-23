"""`code-with-slack`: start the daemon (normally from the LaunchAgent in docs/setup.md)."""

import asyncio
import logging
import signal
import sys
from pathlib import Path

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack.approvals import Approvals
from code_with_slack.config import CONFIG_DIR, ConfigError, load_config
from code_with_slack.footer import UsageCache, UsageProbe
from code_with_slack.guards import ChannelGuard, Identity
from code_with_slack.lock import AlreadyRunning, single_instance
from code_with_slack.sessions import SessionDeps, SessionManager, default_client_factory
from code_with_slack.slack_app import build_app
from code_with_slack.state import StateError, StateStore

logger = logging.getLogger("code_with_slack")


async def run(config_dir: Path = CONFIG_DIR) -> None:
    config = load_config(config_dir)
    with single_instance(config_dir):
        state = StateStore(config_dir / "state.json")
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
        )
        handler = AsyncSocketModeHandler(app, config.app_token)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await handler.connect_async()  # type: ignore[no-untyped-call]  # untyped in Bolt 1.30.0
        logger.info("connected to Slack workspace %s", identity.team_id)
        try:
            await stop.wait()
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
