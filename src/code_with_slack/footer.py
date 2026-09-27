"""The context line under every reply: bypass, branch, changes, model, context, tokens, limits."""

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
# ` 2 files changed, 42 insertions(+), 10 deletions(-)`, git 2.54 (2026-09-27).
SHORTSTAT_INSERTIONS = re.compile(r"(\d+) insertions?\(\+\)")
SHORTSTAT_DELETIONS = re.compile(r"(\d+) deletions?\(-\)")


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


async def _git(cwd: Path, *args: str) -> str | None:
    """`git` run in `cwd`: its output, or None when it fails or outlasts `GIT_TIMEOUT`."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            # The folder is wherever the session went, maybe a repo just cloned: a diff there
            # must not run the repo's own fsmonitor command.
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(cwd),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except TimeoutError:
        return None
    finally:
        # Also when the footer is cancelled (the session closing): no git left unreaped.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return out.decode(errors="replace") if proc.returncode == 0 else None


async def git_branch(cwd: Path) -> str | None:
    out = await _git(cwd, "branch", "--show-current")
    return out.strip() or None if out else None


def shortstat_lines(out: str) -> tuple[int, int]:
    """Insertions and deletions from `git diff --shortstat`, which leaves out a zero count."""
    insertions = SHORTSTAT_INSERTIONS.search(out)
    deletions = SHORTSTAT_DELETIONS.search(out)
    return (int(insertions[1]) if insertions else 0, int(deletions[1]) if deletions else 0)


async def git_changes(cwd: Path) -> tuple[int, int] | None:
    """Lines inserted and deleted since the last commit, staged and unstaged, as ccstatusline's
    git-changes counts them for the terminal (untracked files not counted). None outside a repo."""
    unstaged, staged = await asyncio.gather(
        _git(cwd, "diff", "--shortstat"), _git(cwd, "diff", "--cached", "--shortstat")
    )
    if unstaged is None or staged is None:
        return None
    (added, removed), (added_staged, removed_staged) = map(shortstat_lines, (unstaged, staged))
    return added + added_staged, removed + removed_staged


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
    # The channel's folder: where the owner bound it, whatever folder the session moved to.
    directory: Path | None = None
    # Lines inserted and deleted since the last commit in the folder the session works in, as
    # the branch is; None outside a repo.
    changes: tuple[int, int] | None = None


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
    if hours < 48:
        return f"{hours}h"
    # Days and hours, as the terminal's weekly reset timer (ccstatusline) counts down a week.
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def format_limit(limit: Limit, now: datetime) -> str:
    """`3% ↻ 2h`: the share used, and the time to its reset when known."""
    if limit.resets_at is None:
        return f"{limit.percent}%"
    return f"{limit.percent}% ↻ {format_until(limit.resets_at - now)}"


@dataclass(frozen=True)
class FooterField:
    """One of the footer's values: `!status` shows `label: value`, the footer `short` on its
    `line`: 0 the session, 1 where it works (after the folder), 2 context and limits."""

    label: str
    value: str
    short: str
    line: int = 0


def footer_fields(data: FooterData, now: datetime) -> list[FooterField]:
    """The values the footer and `!status` both show, in the footer's order; what is not known
    is left out. One list, so the two never write a value differently."""
    fields: list[FooterField] = []
    if data.model:
        fields.append(FooterField("Model", data.model, data.model))
    if data.effort:
        fields.append(FooterField("Effort", data.effort, f"effort {data.effort}"))
    if data.session_tokens is not None:
        tokens = format_tokens(data.session_tokens)
        fields.append(FooterField("Session tokens", tokens, f"{tokens} tok"))
    if data.branch:
        fields.append(FooterField("Branch", data.branch, mrkdwn_escape(data.branch), line=1))
    if data.changes is not None:
        changes = f"(+{data.changes[0]},-{data.changes[1]})"
        fields.append(FooterField("Uncommitted", changes, changes, line=1))
    if data.context_percent is not None:
        context = f"{data.context_percent:.0f}%"
        fields.append(FooterField("Context", context, f"ctx {context}", line=2))
    if data.usage and data.usage.session:
        session = format_limit(data.usage.session, now)
        fields.append(FooterField("5h limit", session, f"5h {session}", line=2))
    if data.usage and data.usage.week:
        week = format_limit(data.usage.week, now)
        fields.append(FooterField("7d limit", week, f"7d {week}", line=2))
    return fields


def format_status_fields(data: FooterData, now: datetime) -> list[str]:
    """The footer's values as `!status` lines, one per field; bypass and the folder are left
    out, since the status's Mode and Directory lines already show them."""
    return [f"{field.label}: `{field.value}`" for field in footer_fields(data, now)]


def format_footer(data: FooterData, now: datetime) -> str:
    """Three lines, so a phone never wraps a field (the owner's layout): bypass, model, effort
    and tokens; the channel's folder, branch and changes; context and limits."""
    lines: list[list[str]] = [["⚡ bypass"] if data.bypass else [], [], []]
    if data.directory is not None:
        # Its last two names, as the owner's terminal status line shows a folder.
        names = [part for part in data.directory.parts if part != data.directory.anchor][-2:]
        if names:
            lines[1].append(mrkdwn_escape("/".join(names)))
    for field in footer_fields(data, now):
        lines[field.line].append(field.short)
    return "\n".join(" · ".join(parts) for parts in lines if parts)
