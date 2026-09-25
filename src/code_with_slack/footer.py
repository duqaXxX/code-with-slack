"""The context line under every reply: bypass, branch, model, context, tokens, usage limits."""

import asyncio
import calendar
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from code_with_slack.render.escape import mrkdwn_escape

if TYPE_CHECKING:  # sessions imports this module
    from code_with_slack.sessions import ClaudeClient

logger = logging.getLogger(__name__)

USAGE_TTL = 300.0
# `/usage` answers in seconds; past this the probe gives up, so the footer never stops refreshing.
USAGE_TIMEOUT = 60.0
GIT_TIMEOUT = 5.0
# Wording measured on Claude Code 2.1.280 (2026-09-23). A change hides the field, nothing more.
SESSION_LINE = re.compile(r"^Current session: (\d+)% used(?: · resets (.+))?$", re.M)
WEEK_LINE = re.compile(r"^Current week \(all models\): (\d+)% used(?: · resets (.+))?$", re.M)
RESET = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2}) (?P<day>\d{1,2}) at (?P<hour>\d{1,2})(?::(?P<min>\d{2}))?"
    r"(?P<ampm>am|pm) \((?P<tz>[^)]+)\)$"
)
# Outputs measured on Claude Code 2.1.280 (2026-09-23); the "with X effort" form is the one
# ccstatusline reads from transcripts.
EFFORT_OUTPUT = re.compile(r"^(?:Set effort level to|Effort level set to) ([a-z0-9-]+)", re.I)
MODEL_OUTPUT = re.compile(r"^Set model to\b(?:.*? with ([a-z0-9-]+) effort)?", re.I | re.S)
MONTHS = {name: i for i, name in enumerate(calendar.month_abbr) if name}


@dataclass(frozen=True)
class Limit:
    percent: int
    resets_at: datetime | None


@dataclass(frozen=True)
class Usage:
    session: Limit | None
    week: Limit | None


def parse_reset(text: str, now: datetime) -> datetime | None:
    match = RESET.match(text.strip())
    if match is None or match["mon"] not in MONTHS:
        return None
    hour = int(match["hour"]) % 12 + (12 if match["ampm"] == "pm" else 0)
    try:
        zone = ZoneInfo(match["tz"])
        at = datetime(
            now.year,
            MONTHS[match["mon"]],
            int(match["day"]),
            hour,
            int(match["min"] or 0),
            tzinfo=zone,
        )
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return at.replace(year=at.year + 1) if at < now - timedelta(days=1) else at


def parse_limit(pattern: re.Pattern[str], text: str, now: datetime) -> Limit | None:
    match = pattern.search(text)
    if match is None:
        return None
    return Limit(int(match[1]), parse_reset(match[2], now) if match[2] else None)


def parse_usage(text: str, now: datetime) -> Usage:
    """The 5-hour and weekly figures from `/usage`; a field it cannot read is None."""
    return Usage(parse_limit(SESSION_LINE, text, now), parse_limit(WEEK_LINE, text, now))


class UsageCache:
    def __init__(
        self,
        fetch: Callable[[], Awaitable[str]],
        *,
        ttl: float = USAGE_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()
        self.current: Usage | None = None

    def invalidate(self) -> None:
        self._fetched_at = None

    async def refresh_if_stale(self) -> None:
        """Refresh when older than the TTL; a failure keeps the old value and waits a full TTL."""
        if self._lock.locked():
            return
        async with self._lock:
            if self._fetched_at is not None and self._clock() - self._fetched_at < self._ttl:
                return
            try:
                text = await self._fetch()
                self.current = parse_usage(text, datetime.now().astimezone())
            except Exception as exc:  # the footer is optional: never let it break a reply
                logger.warning("usage refresh failed: %s", type(exc).__name__)
            self._fetched_at = self._clock()


class UsageProbe:
    """A long-lived client whose only job is `/usage`: the limits belong to the account, and a
    probe inside a channel's session would add to that session's transcript."""

    def __init__(
        self, cwd: Path, client_factory: Callable[[ClaudeAgentOptions], "ClaudeClient"]
    ) -> None:
        self._options = ClaudeAgentOptions(cwd=str(cwd), setting_sources=[])
        self._factory = client_factory
        self._client: ClaudeClient | None = None

    async def __call__(self) -> str:
        try:
            async with asyncio.timeout(USAGE_TIMEOUT):
                if self._client is None:
                    self._client = self._factory(self._options)
                    await self._client.connect()
                await self._client.query("/usage")
                text = ""
                async for message in self._client.receive_messages():
                    if isinstance(message, ResultMessage):
                        text = message.result or ""
                        break
                return text
        except Exception:
            # Includes the timeout: a client left mid-query would answer the next one late.
            await self.close()
            raise

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.disconnect()


async def git_branch(cwd: Path) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(cwd),
            "branch",
            "--show-current",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    branch = out.decode(errors="replace").strip()
    return branch if proc.returncode == 0 and branch else None


def effort_change(output: str) -> tuple[bool, str | None]:
    """Whether a command's output changes the effort level, and to what (None: back to unknown).

    `/effort` and `/model` run no Stop hook, the footer's other source (Claude Code 2.1.280), so
    the footer follows their output. A model change without an effort clears the level.
    """
    text = output.strip()
    match = EFFORT_OUTPUT.match(text)
    if match:
        return True, match[1].lower()
    match = MODEL_OUTPUT.match(text)
    if match:
        return True, match[1].lower() if match[1] else None
    return False, None


def session_tokens(result: ResultMessage) -> int | None:
    if not result.model_usage:
        return None
    return sum(
        # A count a model does not report (no cache, say) counts as none.
        u.get("inputTokens", 0)
        + u.get("outputTokens", 0)
        + u.get("cacheReadInputTokens", 0)
        + u.get("cacheCreationInputTokens", 0)
        for u in result.model_usage.values()
    )


@dataclass(frozen=True)
class FooterData:
    bypass: bool
    branch: str | None
    model: str | None
    context_percent: float | None
    session_tokens: int | None
    usage: Usage | None
    effort: str | None = None
    directory: Path | None = None


def format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def format_until(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


def format_footer(data: FooterData, now: datetime) -> str:
    parts: list[str] = []
    if data.bypass:
        parts.append("⚡ bypass")
    if data.branch:
        parts.append(mrkdwn_escape(data.branch))
    if data.model:
        parts.append(data.model)
    if data.effort:
        parts.append(f"effort {data.effort}")
    if data.context_percent is not None:
        parts.append(f"ctx {data.context_percent:.0f}%")
    if data.session_tokens is not None:
        parts.append(f"{format_tokens(data.session_tokens)} tok")
    if data.usage and data.usage.session:
        session = f"5h {data.usage.session.percent}%"
        if data.usage.session.resets_at is not None:
            session += f" ↻ {format_until(data.usage.session.resets_at - now)}"
        parts.append(session)
    if data.usage and data.usage.week:
        parts.append(f"7d {data.usage.week.percent}%")
    if data.directory is not None:
        # Last, and its last two names, as the owner's terminal status line shows the folder.
        names = data.directory.parts[1:][-2:]
        if names:
            parts.append(mrkdwn_escape("/".join(names)))
    return " · ".join(parts)
