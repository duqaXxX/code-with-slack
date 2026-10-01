"""The daemon's only written file: per channel, its bound directory and any pending migration
notice; per thread within it, the folder it was opened in, its session id, bypass switch and
effort level."""

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

logger = logging.getLogger(__name__)

# A thread with no session id whose root message is older than this never finished its first
# turn: `prune` drops it. A Slack `thread_ts` is the root message's epoch time in seconds.
ONE_DAY = 24 * 60 * 60


def _empty_threads() -> Mapping[str, "ThreadState"]:
    return MappingProxyType({})


@dataclass(frozen=True)
class ThreadState:
    directory: Path
    session_id: str | None = None
    # The owner's last word on bypass, kept across restarts (a restart is the daemon's doing, not
    # the owner's): True after `!bypass on` or a ticked setup, False after `!bypass off` or an
    # unticked one, None when never chosen, which leaves Claude Code's own mode alone. On disk,
    # `bypass` keeps its old meaning (`true` on, `false` not on) and an explicit off adds
    # `bypass_off: true`, written for that alone. A file from before reads as never chosen where
    # it held `false`; old code ignores the extra key and reads an off as not on.
    bypass: bool | None = None
    # The level set with `/effort`; `None` when unset or set back to the default.
    effort: str | None = None
    # Crash repair (issue #19), each an id only, never message content:
    # the ts of every open reply's last message (more than one can be open at once: a background
    # task's own reply can outlive the turn that started it). Each `ReplySink` owns one entry:
    # added on its first message, replaced on a continuation, removed once its final write is
    # known to have landed (or it has given up retrying for good).
    open_replies: tuple[str, ...] = ()
    # ts of every approval, question or D8 hold request still carrying buttons.
    requests: tuple[str, ...] = ()
    # The root's reaction name while it is ⏳ or ✋ (`render.status.Status.value`); cleared once
    # ✅ or ❌ is requested.
    status: str | None = None
    # The root's reaction name once ✅ or ❌ is requested, cleared when it turns ⏳ or ✋ again:
    # what the Home tab shows for a session with nothing running. Never read by crash repair.
    # ❌ stands here next to a kept `status` when an answer never reached Slack: the root shows
    # ❌, and repair still owes the thread its notice.
    ended: str | None = None


@dataclass(frozen=True)
class ChannelRecord:
    directory: Path
    # Set by the v1 migration, cleared once the migration notice is posted to the channel.
    notice_pending: bool = False
    threads: Mapping[str, ThreadState] = field(default_factory=_empty_threads)


class StateError(Exception):
    """state.json exists but cannot be read; the daemon refuses to guess."""


def _parse_bypass(raw: dict[str, Any]) -> bool | None:
    if raw.get("bypass") is True:
        return True
    return False if raw.get("bypass_off") is True else None


def _dump_bypass(bypass: bool | None) -> dict[str, bool]:
    """`bypass` as it always was (`true` on, `false` not on), plus `bypass_off: true` for an
    explicit off only: never both set."""
    if bypass:
        return {"bypass": True}
    return {"bypass": False, "bypass_off": True} if bypass is False else {"bypass": False}


def _parse_thread(raw: dict[str, Any]) -> ThreadState:
    """Tolerant of a v2 file written before the repair fields existed (they default to "nothing
    open"), and a v2 file written with them read by code that does not know them yet ignores the
    extra keys: both directions of the additive version stay 2 (`bypass_off` is one such key:
    a file without it reads its `bypass: false` as never chosen; `ended` is another). A file
    written by 1304c5e's single `open_reply` field (never shipped) is read the same as one with
    none at all: that key is not looked at."""
    effort = raw.get("effort")
    open_replies = raw.get("open_replies")
    requests = raw.get("requests")
    status = raw.get("status")
    ended = raw.get("ended")
    return ThreadState(
        directory=Path(raw["directory"]),
        session_id=raw.get("session_id"),
        bypass=_parse_bypass(raw),
        effort=effort if isinstance(effort, str) else None,
        open_replies=tuple(open_replies) if isinstance(open_replies, list) else (),
        requests=tuple(requests) if isinstance(requests, list) else (),
        status=status if isinstance(status, str) else None,
        ended=ended if isinstance(ended, str) else None,
    )


def _parse_v2(raw: dict[str, Any]) -> dict[str, ChannelRecord]:
    channels: dict[str, ChannelRecord] = {}
    for channel_id, entry in raw["channels"].items():
        threads = {thread_ts: _parse_thread(t) for thread_ts, t in entry.get("threads", {}).items()}
        channels[channel_id] = ChannelRecord(
            directory=Path(entry["directory"]),
            notice_pending=entry.get("notice_pending") is True,
            threads=MappingProxyType(threads),
        )
    return channels


def _shown(threads: Mapping[str, ThreadState]) -> dict[str, tuple[str | None, ...]]:
    """What the session index (the Home tab) shows of `threads`: a write that leaves this
    unchanged is not announced to `StateStore.on_sessions_change`."""
    return {ts: (t.session_id, t.status, t.ended) for ts, t in threads.items()}


def _migrate_v1(raw: dict[str, Any]) -> dict[str, ChannelRecord]:
    """Each channel keeps its directory, gets empty threads and a pending notice; the old
    session id and bypass switch belonged to the channel, not to a thread, and are dropped."""
    return {
        channel_id: ChannelRecord(directory=Path(entry["directory"]), notice_pending=True)
        for channel_id, entry in raw["channels"].items()
    }


class StateStore:
    """In-memory copy of state.json, written atomically on every change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._channels = self._load()
        # Called after a write that changed which channels are bound, which threads exist, the
        # session one holds, or a root's reaction: what the session index is built from. Never
        # while loading.
        self.on_sessions_change: Callable[[], None] | None = None

    def channel(self, channel_id: str) -> ChannelRecord | None:
        return self._channels.get(channel_id)

    def channels(self) -> list[str]:
        """The id of every bound channel, in the order they were bound."""
        return list(self._channels)

    def thread(self, channel_id: str, thread_ts: str) -> ThreadState | None:
        channel = self._channels.get(channel_id)
        return channel.threads.get(thread_ts) if channel is not None else None

    def threads(self) -> list[tuple[str, str, ThreadState]]:
        """Every (channel_id, thread_ts, thread), across all channels."""
        return [
            (channel_id, thread_ts, thread)
            for channel_id, channel in self._channels.items()
            for thread_ts, thread in channel.threads.items()
        ]

    def bind(self, channel_id: str, directory: Path) -> None:
        """Bind a channel to a directory; its threads and their own folders stay untouched
        (each thread keeps the folder it was created in), and so does its pending notice."""
        current = self._channels.get(channel_id)
        self._channels[channel_id] = ChannelRecord(
            directory,
            notice_pending=current.notice_pending if current is not None else False,
            threads=current.threads if current is not None else _empty_threads(),
        )
        self._save()
        if current is None:
            self._announce()  # a new channel in the session index; a rebind changes no row

    def open_thread(
        self, channel_id: str, thread_ts: str, session_id: str | None = None
    ) -> ThreadState:
        """Create a thread's entry with the channel's current folder, or return the existing
        one unchanged. Raises `KeyError` for a channel that has never been bound."""
        channel = self._channels[channel_id]
        existing = channel.threads.get(thread_ts)
        if existing is not None:
            return existing
        created = ThreadState(directory=channel.directory, session_id=session_id)
        self._replace_threads(channel_id, {**channel.threads, thread_ts: created})
        return created

    def set_session(self, channel_id: str, thread_ts: str, session_id: str | None) -> None:
        """Record a thread's session id; a no-op for a thread that does not exist."""
        current = self.thread(channel_id, thread_ts)
        if current is not None and current.session_id != session_id:
            self._set_thread(channel_id, thread_ts, replace(current, session_id=session_id))

    def set_bypass(self, channel_id: str, thread_ts: str, on: bool | None) -> None:
        """Record a thread's bypass choice (`None`: never chosen); a no-op for a thread that does
        not exist."""
        current = self.thread(channel_id, thread_ts)
        if current is not None and current.bypass != on:
            self._set_thread(channel_id, thread_ts, replace(current, bypass=on))

    def set_effort(self, channel_id: str, thread_ts: str, effort: str | None) -> None:
        """Record the effort level `/effort` set for a thread; a no-op for an unknown thread."""
        current = self.thread(channel_id, thread_ts)
        if current is not None and current.effort != effort:
            self._set_thread(channel_id, thread_ts, replace(current, effort=effort))

    def replace_open_reply(
        self, channel_id: str, thread_ts: str, old_ts: str | None, new_ts: str | None
    ) -> None:
        """One `ReplySink`'s own entry in the open-replies list (crash repair): drop `old_ts` (if
        it was there), add `new_ts` (if not already there), in one write. A no-op for a thread
        that does not exist, and for a call that changes nothing (both ends of a fresh sink's
        first message, `old_ts=None, new_ts=None`, would otherwise still write)."""
        current = self.thread(channel_id, thread_ts)
        if current is None:
            return
        replies = current.open_replies
        if old_ts is not None and old_ts in replies:
            replies = tuple(t for t in replies if t != old_ts)
        if new_ts is not None and new_ts not in replies:
            replies = (*replies, new_ts)
        if replies != current.open_replies:
            self._set_thread(channel_id, thread_ts, replace(current, open_replies=replies))

    def add_request(self, channel_id: str, thread_ts: str, message_ts: str) -> None:
        """Record a request message still carrying buttons (crash repair); a no-op for a thread
        that does not exist or already has it."""
        current = self.thread(channel_id, thread_ts)
        if current is not None and message_ts not in current.requests:
            self._set_thread(
                channel_id, thread_ts, replace(current, requests=(*current.requests, message_ts))
            )

    def remove_request(self, channel_id: str, thread_ts: str, message_ts: str) -> None:
        """Drop a decided or removed request from the list (crash repair); a no-op if it is not
        there, including for a thread that does not exist."""
        current = self.thread(channel_id, thread_ts)
        if current is not None and message_ts in current.requests:
            remaining = tuple(t for t in current.requests if t != message_ts)
            self._set_thread(channel_id, thread_ts, replace(current, requests=remaining))

    def set_status_pending(
        self, channel_id: str, thread_ts: str, name: str | None, ended: str | None = None
    ) -> None:
        """Record the root's reaction while it is ⏳ or ✋ (`name`), or clear it once ✅ or ❌ is
        requested (crash repair) and keep that one as `ended`; both None for a root left bare,
        both set for a ❌ shown while repair still owes the thread. A no-op for a thread that does
        not exist."""
        current = self.thread(channel_id, thread_ts)
        if current is not None and (current.status, current.ended) != (name, ended):
            self._set_thread(channel_id, thread_ts, replace(current, status=name, ended=ended))

    def repairs_pending(self) -> list[tuple[str, str, ThreadState]]:
        """Every (channel_id, thread_ts, thread) whose crash-repair fields are not all empty:
        what a crashed daemon left open for `run`'s startup repair to find."""
        return [
            (channel_id, thread_ts, thread)
            for channel_id, channel in self._channels.items()
            for thread_ts, thread in channel.threads.items()
            if thread.open_replies or thread.requests or thread.status is not None
        ]

    def clear_repair(self, channel_id: str, thread_ts: str, *, keep_open: bool = False) -> None:
        """Clear a thread's crash-repair fields together, once it has been repaired (or repair
        failed for good and was logged); a no-op if already clear or the thread is gone. With
        `keep_open` the open replies and the root's status stay: an answer never reached Slack,
        and the next start's repair still has to close it and say so."""
        current = self.thread(channel_id, thread_ts)
        if current is None:
            return
        replies = current.open_replies if keep_open else ()
        status = current.status if keep_open else None
        if not current.requests and current.open_replies == replies and current.status == status:
            return
        self._set_thread(
            channel_id,
            thread_ts,
            replace(current, open_replies=replies, requests=(), status=status),
        )

    def remove_thread(self, channel_id: str, thread_ts: str) -> None:
        """Drop a thread's entry; a no-op if it is not there."""
        channel = self._channels.get(channel_id)
        if channel is None or thread_ts not in channel.threads:
            return
        remaining = {ts: t for ts, t in channel.threads.items() if ts != thread_ts}
        self._replace_threads(channel_id, remaining)

    def holder(self, session_id: str) -> tuple[str, str] | None:
        """The (channel_id, thread_ts) whose thread holds this session id, across all channels."""
        for channel_id, channel in self._channels.items():
            for thread_ts, thread in channel.threads.items():
                if thread.session_id == session_id:
                    return channel_id, thread_ts
        return None

    def pending_notices(self) -> list[str]:
        """Channel ids whose v1-to-v2 migration notice has not been posted yet."""
        return [channel_id for channel_id, c in self._channels.items() if c.notice_pending]

    def clear_notice(self, channel_id: str) -> None:
        """Mark a channel's migration notice as posted; a no-op if already clear or unbound."""
        channel = self._channels.get(channel_id)
        if channel is None or not channel.notice_pending:
            return
        self._channels[channel_id] = replace(channel, notice_pending=False)
        self._save()

    def prune(self, alive: Callable[[Path], Collection[str] | None], now: float) -> int:
        """Remove a thread whose session id is gone from its folder's sessions, and a
        no-session thread whose root message is older than `ONE_DAY`. Calls `alive` once per
        distinct folder; None means it cannot tell, and that folder's threads are kept. If
        `alive` raises, nothing is removed or written; the caller decides what to do next.
        Returns how many entries were removed."""
        alive_cache: dict[Path, Collection[str] | None] = {}
        updated: dict[str, ChannelRecord] = {}
        removed = 0
        for channel_id, channel in self._channels.items():
            kept: dict[str, ThreadState] = {}
            for thread_ts, thread in channel.threads.items():
                if thread.session_id is not None:
                    if thread.directory not in alive_cache:
                        alive_cache[thread.directory] = alive(thread.directory)
                    sessions = alive_cache[thread.directory]
                    if sessions is not None and thread.session_id not in sessions:
                        removed += 1
                        continue
                elif now - float(thread_ts) > ONE_DAY:
                    removed += 1
                    continue
                kept[thread_ts] = thread
            updated[channel_id] = (
                channel
                if len(kept) == len(channel.threads)
                else replace(channel, threads=MappingProxyType(kept))
            )
        if removed:
            self._channels = updated
            self._save()
            self._announce()
        return removed

    def _set_thread(self, channel_id: str, thread_ts: str, updated: ThreadState) -> None:
        channel = self._channels[channel_id]
        self._replace_threads(channel_id, {**channel.threads, thread_ts: updated})

    def _replace_threads(self, channel_id: str, threads: dict[str, ThreadState]) -> None:
        channel = self._channels[channel_id]
        self._channels[channel_id] = replace(channel, threads=MappingProxyType(threads))
        self._save()
        if _shown(channel.threads) != _shown(threads):
            self._announce()

    def _announce(self) -> None:
        """The write is already on disk: an observer that fails must not fail its caller."""
        if self.on_sessions_change is None:
            return
        try:
            self.on_sessions_change()
        except Exception as exc:
            logger.warning("the session index was not told of a change: %s", type(exc).__name__)

    def _load(self) -> dict[str, ChannelRecord]:
        try:
            text = self._path.read_text()
        except FileNotFoundError:
            return {}
        try:
            raw = json.loads(text)
            version = raw["version"]
            if version == 2:
                return _parse_v2(raw)
            if version == 1:
                channels = _migrate_v1(raw)
                self._write(channels)
                return channels
            raise StateError(f"{self._path} has an unknown version; fix or delete it")
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            raise StateError(f"{self._path} cannot be read ({exc}); fix or delete it") from exc

    def _save(self) -> None:
        self._write(self._channels)

    def _write(self, channels: dict[str, ChannelRecord]) -> None:
        data = {
            "version": 2,
            "channels": {
                channel_id: {
                    "directory": str(c.directory),
                    "notice_pending": c.notice_pending,
                    "threads": {
                        thread_ts: {
                            "directory": str(t.directory),
                            "session_id": t.session_id,
                            **_dump_bypass(t.bypass),
                            "effort": t.effort,
                            "open_replies": list(t.open_replies),
                            "requests": list(t.requests),
                            "status": t.status,
                            "ended": t.ended,
                        }
                        for thread_ts, t in c.threads.items()
                    },
                }
                for channel_id, c in channels.items()
            },
        }
        # Write beside the target and rename: a crash leaves the old file or the new one.
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        dir_fd = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
