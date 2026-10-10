"""Commands typed as messages: `!word`. A few words are awaydesk's own; any other
`!name args` is a Claude Code command, so a command a new Claude Code release adds works at once.

Slack never delivers a message that starts with `/` to the bot (measured 2026-09-23), so
Claude Code's own slash is replaced by `!`, as the official Claude app for Slack does with its
`@Claude !word` commands.
"""

from dataclasses import dataclass
from typing import Any, ClassVar

from awaydesk import texts
from awaydesk.render.escape import markdown_escape


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
class Open:
    WORD: ClassVar[str] = "open"
    target: str = ""  # a path or words in one; empty offers a picker


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
Word = Help | Guide | Bind | Bypass | Status | Stop | Resume | Open | Invalid
Command = Word | Passthrough
DESCRIPTION_LIMIT = 100
# Claude Code's `/clear` starts a new session under any of its names (aliases `/reset` and `/new`:
# commands reference, code.claude.com/docs/en/commands, read 2026-10-02, and the list SDK 0.2.163
# reports), and one thread is one session: the daemon refuses each of them inside a thread, which
# is also the only place a session's commands are listed. Named here because a session rebuilt
# after a restart lists no command until it connects (`refused_in_thread`).
NEW_SESSION_NAMES = frozenset({"clear", "reset", "new"})
# Claude Code's `/login` and `/logout` act on the host's own login, which the daemon and every
# session run on: neither is ever sent to Claude Code, from the channel or from a thread, and
# each is answered with where it is done instead (`host_only`).
HOST_ONLY = {"login": texts.LOGIN_ON_HOST, "logout": texts.LOGOUT_ON_HOST}


def parse_bang(text: str) -> Command | None:
    """Map a `!word` message to the daemon's own command or a Claude Code passthrough;
    None when the message is not a command at all."""
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None
    body = stripped[1:]
    if not body or body[0].isspace():
        return None
    # The word ends at any whitespace: a line break after it (a code block that follows the
    # word, a second line) separates it from its arguments as a space does.
    word, *more = body.split(None, 1)
    rest = more[0].strip() if more else ""
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
        case "open":
            return Open(rest)
    return Passthrough(f"{word} {rest}".strip())


# The parts of a composer message a word is read through. A quote or a list is left out: what
# opens it is not what the owner typed first.
_READ_THROUGH = ("rich_text_section", "rich_text_preformatted")
_MARKS = "`*_~"  # what Slack writes in a message's text around code, bold, italic, strikethrough


def _first_run(blocks: object) -> str | None:
    """The text a composer message opens with, without its formatting; None for a message that
    opens with anything else (a quote, a list, an emoji, a mention) or has no composer block."""
    block = blocks[0] if isinstance(blocks, list) and blocks else {}
    part = (block.get("elements") or [{}])[0] if block.get("type") == "rich_text" else {}
    leaf = (part.get("elements") or [{}])[0] if part.get("type") in _READ_THROUGH else {}
    return str(leaf.get("text", "")) if leaf.get("type") == "text" else None


def unformatted(text: str, blocks: object) -> str:
    """`text` without the marks around the run it opens with, when that run starts with `!`: the
    composer's block says what the run is (`_first_run`), the text holds the rest as the owner
    sent it. Empty when the message does not open with a formatted `!`, or when the text does
    not read as its marks, that run, then the same marks closing it or the message."""
    run = _first_run(blocks)
    opening, bang, rest = text.strip().partition("!")
    body = bang + rest
    if not run or not run.startswith("!") or not opening or set(opening) - set(_MARKS):
        return ""
    if not body.startswith(run):
        return ""
    after, closing = body[len(run) :], opening[::-1]
    if after.startswith(closing):
        return run + after[len(closing) :]
    if after.endswith(closing):
        return run + after[: -len(closing)]
    return ""


def help_text(commands: list[dict[str, Any]] | None, query: str = "") -> str:
    """The daemon's words, then every command the session offers now (None: not bound yet)
    except those its thread refuses (`refused_in_thread`), keeping only the lines whose name or
    description contains `query`, ignoring case. With no query, the rule that tells a command
    from a text comes first."""

    def keep(line: str) -> bool:
        return query.lower() in line.lower()

    own = [line for line in texts.HELP_WORDS if keep(line)]
    refused = refused_in_thread(commands)
    offered = [c for c in commands or [] if command_name(c).lower() not in refused]
    session = [
        f"{usage} {markdown_escape(description)}".rstrip()
        for usage, description in map(command_parts, sorted(offered, key=command_name))
        if keep(f"{usage} {description}")
    ]
    # The rule heads the whole list; a search shows its matches alone.
    lines = [texts.HELP_OWN, *([] if query else [texts.HELP_RULE]), *own]
    if commands is None:
        lines.append(texts.HELP_UNBOUND)
    else:
        lines += [texts.HELP_CLAUDE, *session]
    if query and not own and not session:
        lines.append(texts.HELP_NO_MATCH.format(query=query))
    return "\n".join(lines)


def host_only(command: Passthrough) -> str | None:
    """The answer to a command that is only ever run on the host (HOST_ONLY), or None for any
    other: the caller sends that answer and nothing to Claude Code."""
    return HOST_ONLY.get(command.text.split(" ", 1)[0].lower())


def refused_in_thread(commands: list[dict[str, Any]] | None) -> frozenset[str]:
    """The command names a thread refuses, lowercase: NEW_SESSION_NAMES, with every alias the
    session's own list (`commands`, None or empty before it connects) gives one of them."""
    aliases = {
        str(alias).lower()
        for command in commands or []
        if command_name(command).lower() in NEW_SESSION_NAMES
        for alias in command.get("aliases") or []
    }
    return NEW_SESSION_NAMES | aliases


def command_name(command: dict[str, Any]) -> str:
    return str(command.get("name", ""))


def command_parts(command: dict[str, Any]) -> tuple[str, str]:
    """`!name hint` and the description as written, cut to one short line; not escaped. The
    description is third-party text, and a cut can fall inside its own code span: the caller
    escapes it before showing it (`help_text`)."""
    name = command_name(command)
    hint = str(command.get("argumentHint") or "")
    usage = f"!{name} {hint}".rstrip()
    # A code span cannot hold a backtick: such a usage is plain text, escaped.
    usage = markdown_escape(usage) if "`" in usage else f"`{usage}`"
    description = " ".join(str(command.get("description") or "").split())
    if len(description) > DESCRIPTION_LIMIT:
        description = description[: DESCRIPTION_LIMIT - 1] + "…"
    return usage, description
