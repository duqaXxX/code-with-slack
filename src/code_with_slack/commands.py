"""Commands typed as messages: `!word`. A few words are code-with-slack's own; any other
`!name args` is a Claude Code command, so a command a new Claude Code release adds works at once.

Slack never delivers a message that starts with `/` to the bot (measured 2026-09-23), so
Claude Code's own slash is replaced by `!`, as the official Claude app for Slack does with its
`@Claude !word` commands.
"""

from dataclasses import dataclass
from typing import Any

from code_with_slack import texts


@dataclass(frozen=True)
class Help:
    pass


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
class Passthrough:
    text: str


@dataclass(frozen=True)
class Invalid:
    pass


Command = Help | Bind | Bypass | Status | Stop | Passthrough | Invalid
DESCRIPTION_LIMIT = 100


def parse_bang(text: str) -> Command | None:
    """Map a `!word` message to the daemon's own command or a Claude Code passthrough;
    None when the message is not a command at all."""
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None
    body = stripped[1:]
    word, _, rest = body.partition(" ")
    if not word:
        return None
    rest = rest.strip()
    match word.lower():
        case "help" if not rest:
            return Help()
        case "bind":
            return Bind(rest) if rest else Invalid()
        case "bypass":
            return {"on": Bypass(True), "off": Bypass(False)}.get(rest.lower(), Invalid())
        case "status" if not rest:
            return Status()
        case "stop" if not rest:
            return Stop()
    return Passthrough(body)


def help_text(commands: list[dict[str, Any]] | None) -> str:
    """The daemon's words, then every command the session offers now (None: not bound yet)."""
    lines = [texts.HELP_OWN]
    if commands is None:
        lines.append(texts.HELP_UNBOUND)
        return "\n".join(lines)
    lines.append(texts.HELP_CLAUDE)
    for command in sorted(commands, key=lambda c: str(c.get("name", ""))):
        name = str(command.get("name", ""))
        hint = str(command.get("argumentHint") or "")
        usage = f"`!{name} {hint}`" if hint else f"`!{name}`"
        description = " ".join(str(command.get("description") or "").split())
        if len(description) > DESCRIPTION_LIMIT:
            description = description[: DESCRIPTION_LIMIT - 1] + "…"
        lines.append(f"{usage} {description}".rstrip())
    return "\n".join(lines)
