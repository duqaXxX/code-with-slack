"""The daemon's only written file: per channel, its bound directory and any pending migration
notice; per thread within it, the folder it was opened in, its session id, bypass switch and
effort level."""

import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

# A thread with no session id whose root message is older than this never finished its first
# turn: `prune` drops it. A Slack `thread_ts` is the root message's epoch time in seconds.
ONE_DAY = 24 * 60 * 60


def _empty_threads() -> Mapping[str, "ThreadState"]:
    return MappingProxyType({})


@dataclass(frozen=True)
class ThreadState:
    directory: Path
    session_id: str | None = None
    # `!bypass on`, kept across restarts: a restart is the daemon's doing, not the owner's.
    bypass: bool = False
    # The level set with `/effort`; `None` when unset or set back to the default.
    effort: str | None = None
    # Crash repair (issue #19), each an id only, never message content:
    # the ts of the open reply's last message, updated on every continuation, cleared once the
    # reply closes out or is closed silently.
    open_reply: str | None = None
    # ts of every approval, question or D8 hold request still carrying buttons.
    requests: tuple[str, ...] = ()
    # The root's reaction name while it is ⏳ or ✋ (`render.status.Status.value`); cleared once
    # ✅ or ❌ is requested.
    status: str | None = None


@dataclass(frozen=True)
class ChannelRecord:
    directory: Path
    # Set by the v1 migration, cleared once the migration notice is posted to the channel.
    notice_pending: bool = False
    threads: Mapping[str, ThreadState] = field(default_factory=_empty_threads)


class StateError(Exception):
    """state.json exists but cannot be read; the daemon refuses to guess."""


def _parse_thread(raw: dict[str, Any]) -> ThreadState:
    """Tolerant of a v2 file written before the repair fields existed (they default to "nothing
    open"), and a v2 file written with them read by code that does not know them yet ignores the
    extra keys: both directions of the additive version stay 2."""
    effort = raw.get("effort")
    open_reply = raw.get("open_reply")
    requests = raw.get("requests")
    status = raw.get("status")
    return ThreadState(
        directory=Path(raw["directory"]),
        session_id=raw.get("session_id"),
        bypass=raw.get("bypass") is True,
        effort=effort if isinstance(effort, str) else None,
        open_reply=open_reply if isinstance(open_reply, str) else None,
        requests=tuple(requests) if isinstance(requests, list) else (),
        status=status if isinstance(status, str) else None,
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

    def channel(self, channel_id: str) -> ChannelRecord | None:
        return self._channels.get(channel_id)

    def thread(self, channel_id: str, thread_ts: str) -> ThreadState | None:
        channel = self._channels.get(channel_id)
        return channel.threads.get(thread_ts) if channel is not None else None

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

    def set_bypass(self, channel_id: str, thread_ts: str, on: bool) -> None:
        """Record a thread's bypass switch; a no-op for a thread that does not exist."""
        current = self.thread(channel_id, thread_ts)
        if current is not None and current.bypass != on:
            self._set_thread(channel_id, thread_ts, replace(current, bypass=on))

    def set_effort(self, channel_id: str, thread_ts: str, effort: str | None) -> None:
        """Record the effort level `/effort` set for a thread; a no-op for an unknown thread."""
        current = self.thread(channel_id, thread_ts)
        if current is not None and current.effort != effort:
            self._set_thread(channel_id, thread_ts, replace(current, effort=effort))

    def set_open_reply(self, channel_id: str, thread_ts: str, message_ts: str | None) -> None:
        """Record the ts of an open reply's last message (crash repair); a no-op for a thread
        that does not exist."""
        current = self.thread(channel_id, thread_ts)
        if current is not None and current.open_reply != message_ts:
            self._set_thread(channel_id, thread_ts, replace(current, open_reply=message_ts))

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

    def set_status_pending(self, channel_id: str, thread_ts: str, name: str | None) -> None:
        """Record the root's reaction while it is ⏳ or ✋ (`name`), or clear it once ✅ or ❌ is
        requested (crash repair); a no-op for a thread that does not exist."""
        current = self.thread(channel_id, thread_ts)
        if current is not None and current.status != name:
            self._set_thread(channel_id, thread_ts, replace(current, status=name))

    def repairs_pending(self) -> list[tuple[str, str, ThreadState]]:
        """Every (channel_id, thread_ts, thread) whose crash-repair fields are not all empty:
        what a crashed daemon left open for `run`'s startup repair to find."""
        return [
            (channel_id, thread_ts, thread)
            for channel_id, channel in self._channels.items()
            for thread_ts, thread in channel.threads.items()
            if thread.open_reply is not None or thread.requests or thread.status is not None
        ]

    def clear_repair(self, channel_id: str, thread_ts: str) -> None:
        """Clear a thread's three crash-repair fields together, once it has been repaired (or
        repair failed for good and was logged); a no-op if already clear or the thread is gone."""
        current = self.thread(channel_id, thread_ts)
        if current is None:
            return
        if current.open_reply is None and not current.requests and current.status is None:
            return
        self._set_thread(
            channel_id, thread_ts, replace(current, open_reply=None, requests=(), status=None)
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
        return removed

    def _set_thread(self, channel_id: str, thread_ts: str, updated: ThreadState) -> None:
        channel = self._channels[channel_id]
        self._replace_threads(channel_id, {**channel.threads, thread_ts: updated})

    def _replace_threads(self, channel_id: str, threads: dict[str, ThreadState]) -> None:
        channel = self._channels[channel_id]
        self._channels[channel_id] = replace(channel, threads=MappingProxyType(threads))
        self._save()

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
                            "bypass": t.bypass,
                            "effort": t.effort,
                            "open_reply": t.open_reply,
                            "requests": list(t.requests),
                            "status": t.status,
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
