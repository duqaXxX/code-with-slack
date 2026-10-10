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

from awaydesk.render.escape import mrkdwn_escape
from awaydesk.trust import Repository

if TYPE_CHECKING:  # sessions imports this module
    from awaydesk.sessions import ClaudeClient

logger = logging.getLogger(__name__)

USAGE_TTL = 300.0
# `/usage` answers in seconds; past this the probe gives up, so the footer never stops refreshing.
USAGE_TIMEOUT = 60.0
# For every git call of one footer together: the reply waits for it.
GIT_TIMEOUT = 5.0
# The footer's fields that come before the folder.
SESSION = ("Model", "Effort")
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
# ` 2 files changed, 42 insertions(+), 10 deletions(-)`, as every --shortstat writes it, git 2.54
# (2026-09-27).
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


async def run_git(repository: Repository, *args: str) -> str | None:
    """`git` on `repository`: its output, or None when it fails. The caller holds the time
    limit (`git_state` here, `awaydesk.openfile` for `!open`), and chooses commands that
    never write the index (see `_changes`)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            # A diff must not run the repo's own fsmonitor command.
            "-c",
            "core.fsmonitor=false",
            # Named, never found: git left to search from the folder would take a planted `.git`
            # file, a bare layout or a `core.worktree` at its word (git(1), `--git-dir`).
            "--git-dir",
            str(repository.git_dir),
            *args,
            cwd=repository.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = await proc.communicate()
    finally:
        # Also when the time limit or the session closing cancels the footer: no git left
        # unreaped.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return out.decode(errors="replace") if proc.returncode == 0 else None


def shortstat_lines(out: str) -> tuple[int, int]:
    """Insertions and deletions from a `--shortstat`, which leaves out a zero count."""
    insertions = SHORTSTAT_INSERTIONS.search(out)
    deletions = SHORTSTAT_DELETIONS.search(out)
    return (int(insertions[1]) if insertions else 0, int(deletions[1]) if deletions else 0)


async def _changes(repository: Repository) -> tuple[int, int] | None:
    """Lines inserted and deleted since the last commit, staged and unstaged, as ccstatusline's
    git-changes counts them for the terminal (untracked files not counted). None where there is
    no work tree.

    Plumbing only: `git diff` refreshes and rewrites the index under `index.lock` (measured on
    git 2.54, `--no-optional-locks` included), and a lock left by a killed diff would stop every
    `git add` and commit in the repo. `diff-files` and `diff-index` never write it.

    A submodule counts by its commit alone. Without `--ignore-submodules=dirty` git runs
    `git status` inside every nested repository the index names, under that repository's own
    config and filters (measured on git 2.54, 2026-10-04).
    """
    unstaged = await run_git(repository, "diff-files", "--shortstat", "--ignore-submodules=dirty")
    if unstaged is None:
        return None
    # `--`: git runs at the root, where a file named HEAD would make the revision ambiguous.
    staged = await run_git(repository, "diff-index", "--cached", "--shortstat", "HEAD", "--")
    if staged is None:
        # No commit yet: what is staged is compared with the empty tree, as `git diff --cached`.
        empty = await run_git(repository, "hash-object", "-t", "tree", "/dev/null")
        if empty is None:
            return None
        staged = await run_git(repository, "diff-index", "--cached", "--shortstat", empty.strip())
        if staged is None:
            return None
    (added, removed), (added_staged, removed_staged) = map(shortstat_lines, (unstaged, staged))
    return added + added_staged, removed + removed_staged


async def git_state(
    here: Path, repository: Callable[[Path], Awaitable[Repository | None]]
) -> tuple[str | None, tuple[int, int] | None]:
    """The branch and the changes of the repository holding `here`, each None when unknown.

    `repository` answers only for a repository the owner trusted in Claude Code, or one inside the
    folder the session started in (`awaydesk.trust.trusted_repository`): anywhere else no
    git runs, since a diff runs the filters a repository's config names. Every call shares one
    `GIT_TIMEOUT`.
    """
    branch: str | None = None
    changes: tuple[int, int] | None = None
    try:
        async with asyncio.timeout(GIT_TIMEOUT):
            found = await repository(here)
            if found is None or found.git_dir is None:
                return None, None
            out = await run_git(found, "branch", "--show-current")
            branch = out.strip() or None if out else None
            if not found.inside_git_dir:
                changes = await _changes(found)
    except TimeoutError:
        pass
    return branch, changes


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
    """One of the footer's values: `!status` shows `label: value`, the footer `short`, whose
    label is bold (`*ctx* 15%`, the owner's choice)."""

    label: str
    value: str
    short: str


def footer_fields(data: FooterData, now: datetime) -> list[FooterField]:
    """The values the footer and `!status` both show, in the footer's order; what is not known
    is left out. One list, so the two never write a value differently."""
    fields: list[FooterField] = []
    if data.model:
        fields.append(FooterField("Model", data.model, data.model))
    if data.effort:
        fields.append(FooterField("Effort", data.effort, f"*effort* {data.effort}"))
    if data.branch:
        fields.append(FooterField("Branch", data.branch, mrkdwn_escape(data.branch)))
    if data.changes is not None:
        changes = f"(+{data.changes[0]},-{data.changes[1]})"
        fields.append(FooterField("Uncommitted", changes, changes))
    if data.session_tokens is not None:
        tokens = format_tokens(data.session_tokens)
        fields.append(FooterField("Session tokens", tokens, f"{tokens} *tok*"))
    if data.context_percent is not None:
        context = f"{data.context_percent:.0f}%"
        fields.append(FooterField("Context", context, f"*ctx* {context}"))
    if data.usage and data.usage.session:
        session = format_limit(data.usage.session, now)
        fields.append(FooterField("5h limit", session, f"*5h* {session}"))
    if data.usage and data.usage.week:
        week = format_limit(data.usage.week, now)
        fields.append(FooterField("7d limit", week, f"*7d* {week}"))
    return fields


def format_status_fields(data: FooterData, now: datetime) -> list[str]:
    """The footer's values as `!status` lines, one per field; bypass and the folder are left
    out, since the status's Mode and Directory lines already show them."""
    return [f"{field.label}: `{field.value}`" for field in footer_fields(data, now)]


def format_footer(data: FooterData, now: datetime) -> str:
    """One line: bypass, model and effort, the channel's folder, then branch, changes, tokens,
    context and limits (the owner's order)."""
    fields = footer_fields(data, now)
    parts = ["⚡ bypass"] if data.bypass else []
    parts += [f.short for f in fields if f.label in SESSION]
    if data.directory is not None and data.directory.name:
        # Its name alone, the project's: the whole path is on `!status`'s Directory line.
        parts.append(mrkdwn_escape(data.directory.name))
    parts += [f.short for f in fields if f.label not in SESSION]
    return " · ".join(parts)
