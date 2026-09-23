"""The two ways a Claude Code command reaches a session: `/cc <command>` and `!<command>`.

Slack never delivers a message that starts with `/` to the bot (measured 2026-09-23), so
Claude Code's own slash is replaced by one of these.
"""

from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True)
class Bind:
    path: str


@dataclass(frozen=True)
class Bypass:
    on: bool


@dataclass(frozen=True)
class Status:
    pass


@dataclass(frozen=True)
class Stop:
    pass


@dataclass(frozen=True)
class Picker:
    pass


@dataclass(frozen=True)
class Passthrough:
    text: str


@dataclass(frozen=True)
class Invalid:
    pass


Command = Bind | Bypass | Status | Stop | Picker | Passthrough | Invalid


def parse_cc(text: str) -> Command:
    """Map the text after `/cc` to the daemon's own command or a Claude Code passthrough."""
    stripped = text.strip().removeprefix("/")
    if not stripped:
        return Picker()
    word, _, rest = stripped.partition(" ")
    rest = rest.strip()
    match word.lower():
        case "bind":
            return Bind(rest) if rest else Invalid()
        case "bypass":
            return {"on": Bypass(True), "off": Bypass(False)}.get(rest.lower(), Invalid())
        case "status" if not rest:
            return Status()
        case "stop" if not rest:
            return Stop()
    return Passthrough(stripped)


def bang_command(text: str, known: Collection[str]) -> str | None:
    """`!name args` as `name args` when `name` is a command the session offers, else None."""
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None
    body = stripped[1:]
    name = body.split(" ", 1)[0]
    return body if name and name in known else None
