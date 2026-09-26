"""The daemon's only written file: the directory, the session id and the bypass switch of each
bound channel."""

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class ChannelState:
    directory: Path
    session_id: str | None = None
    # `!bypass on`, kept across restarts: a restart is the daemon's doing, not the owner's.
    bypass: bool = False


class StateError(Exception):
    """state.json exists but cannot be read; the daemon refuses to guess."""


class StateStore:
    """In-memory copy of state.json, written atomically on every change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._channels = self._load()

    def get(self, channel_id: str) -> ChannelState | None:
        return self._channels.get(channel_id)

    def bind(self, channel_id: str, directory: Path) -> None:
        """Bind a channel to a directory; the old session and its bypass stay with the old one."""
        self._channels[channel_id] = ChannelState(directory)
        self._save()

    def set_session(self, channel_id: str, session_id: str | None) -> None:
        """Record a bound channel's session; a channel with no directory has none to record."""
        current = self._channels.get(channel_id)
        if current is not None and current.session_id != session_id:
            self._channels[channel_id] = replace(current, session_id=session_id)
            self._save()

    def set_bypass(self, channel_id: str, on: bool) -> None:
        """Record a bound channel's bypass switch; a resumed session keeps it."""
        current = self._channels.get(channel_id)
        if current is not None and current.bypass != on:
            self._channels[channel_id] = replace(current, bypass=on)
            self._save()

    def _load(self) -> dict[str, ChannelState]:
        try:
            raw = json.loads(self._path.read_text())
            if raw.get("version") != 1:
                raise StateError(f"{self._path} has an unknown version; fix or delete it")
            return {
                # Only a literal true switches bypass on: a hand-edited "false" must not.
                channel: ChannelState(
                    Path(entry["directory"]), entry.get("session_id"), entry.get("bypass") is True
                )
                for channel, entry in raw["channels"].items()
            }
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            raise StateError(f"{self._path} cannot be read ({exc}); fix or delete it") from exc

    def _save(self) -> None:
        data = {
            "version": 1,
            "channels": {
                channel: {
                    "directory": str(s.directory),
                    "session_id": s.session_id,
                    "bypass": s.bypass,
                }
                for channel, s in self._channels.items()
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
