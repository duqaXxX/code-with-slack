"""One daemon at a time: Socket Mode spreads events across every open connection."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(Exception):
    """Another code-with-slack process holds the lock."""


@contextmanager
def single_instance(directory: Path) -> Iterator[None]:
    """Hold an exclusive flock on the config directory itself, so the lock adds no file to it.

    The kernel drops the lock when the process dies, so a crash never leaves a stale lock.
    """
    fd = os.open(directory, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            message = f"another code-with-slack is running (lock on {directory})"
            raise AlreadyRunning(message) from None
        yield
    finally:
        os.close(fd)
