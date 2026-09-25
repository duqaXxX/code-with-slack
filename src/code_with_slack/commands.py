"""Commands typed as messages: `!word`. A few words are code-with-slack's own; any other
`!name args` is a Claude Code command, so a command a new Claude Code release adds works at once.

Slack never delivers a message that starts with `/` to the bot (measured 2026-09-23), so
Claude Code's own slash is replaced by `!`, as the official Claude app for Slack does with its
`@Claude !word` commands.
"""

from dataclasses import dataclass
from typing import Any, ClassVar

from code_with_slack import texts
from code_with_slack.render.escape import markdown_escape


@dataclass(frozen=True)
class Help:
    WORD: ClassVar[str] = "help"
    query: str = ""  # lowercase; empty lists everything


@dataclass(frozen=True)
class Bind:
    WORD: ClassVar[str] = "bind"
    path: str


@dataclass(frozen=True)
class Bypass:
    WORD: ClassVar[str] = "bypass"
    on: bool


@dataclass(frozen=True)
class Status:
    WORD: ClassVar[str] = "status"


@dataclass(frozen=True)
class Stop:
    WORD: ClassVar[str] = "stop"


@dataclass(frozen=True)
class Resume:
    WORD: ClassVar[str] = "resume"
    target: str = ""  # a session id or name; empty lists the sessions


@dataclass(frozen=True)
class Guide:
    WORD: ClassVar[str] = "guide"


@dataclass(frozen=True)
class Passthrough:
    text: str


@dataclass(frozen=True)
class Invalid:
    pass


# The daemon's own words, answered without Claude Code; a Passthrough goes to the session. Each
# word's class names it in WORD, and tests/test_commands.py checks that the guide (`!guide`)
# and `!help` explain every one: a new word cannot ship without its line in both.
Word = Help | Guide | Bind | Bypass | Status | Stop | Resume | Invalid
Command = Word | Passthrough
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
        case "help":
            return Help(rest.lower())
        case "guide" if not rest:
            return Guide()
        case "bind":
            return Bind(rest)  # alone, it lists the folders a session can start in
        case "bypass":
            return {"on": Bypass(True), "off": Bypass(False)}.get(rest.lower(), Invalid())
        case "status" if not rest:
            return Status()
        case "stop" if not rest:
            return Stop()
        case "resume":
            return Resume(rest)
    return Passthrough(body)


def help_text(commands: list[dict[str, Any]] | None, query: str = "") -> str:
    """The daemon's words, then every command the session offers now (None: not bound yet),
    keeping only the lines whose name or description contains `query`, ignoring case."""

    def keep(line: str) -> bool:
        return query.lower() in line.lower()

    own = [line for line in texts.HELP_WORDS if keep(line)]
    session = [
        f"{usage} {markdown_escape(description)}".rstrip()
        for usage, description in map(command_parts, sorted(commands or [], key=command_name))
        if keep(f"{usage} {description}")
    ]
    lines = [texts.HELP_OWN, *own]
    if commands is None:
        lines.append(texts.HELP_UNBOUND)
    else:
        lines += [texts.HELP_CLAUDE, *session]
    if query and not own and not session:
        lines.append(texts.HELP_NO_MATCH.format(query=query))
    return "\n".join(lines)


def command_name(command: dict[str, Any]) -> str:
    return str(command.get("name", ""))


def command_parts(command: dict[str, Any]) -> tuple[str, str]:
    """`!name hint` and the description as written, cut to one short line. The description is
    third-party text: shown, it is escaped, since a cut can fall inside its own code span."""
    name = command_name(command)
    hint = str(command.get("argumentHint") or "")
    usage = f"!{name} {hint}".rstrip()
    # A code span cannot hold a backtick: such a usage is plain text, escaped.
    usage = markdown_escape(usage) if "`" in usage else f"`{usage}`"
    description = " ".join(str(command.get("description") or "").split())
    if len(description) > DESCRIPTION_LIMIT:
        description = description[: DESCRIPTION_LIMIT - 1] + "…"
    return usage, description
