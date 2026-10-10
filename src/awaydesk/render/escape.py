"""Third-party text shown as written in Slack's two markup languages. A leaf module, so the
footer, the sinks, the approvals and the lists can all use it without an import cycle."""

import re

# The characters that start inline formatting in a markdown block, each escapable with a backslash
# (Slack's markdown block reference, read 2026-09-25). `#`, `+`, `-`, `.` and `!` act only at the
# start of a line. `~` is not in the reference's list, but `~~` renders as strikethrough (measured
# live 2026-09-25); `<` stays text there.
MARKDOWN_INLINE = re.compile(r"([\\`*_{}\[\]()&~])")


def mrkdwn_escape(text: str) -> str:
    """Slack's mrkdwn reads `&`, `<` and `>` as markup; a tool's title is shown as written."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def markdown_escape(text: str) -> str:
    """Text for inside a markdown block's own formatting, shown as written."""
    return MARKDOWN_INLINE.sub(r"\\\1", text)


def shown_as_written(text: str) -> str:
    """Model-written text for mrkdwn, shown as written: `&`, `<` and `>` escaped, so no
    `<url|label>` can hide what it links, and a zero-width space after each backtick, so no
    run of three can close a code block."""
    return mrkdwn_escape(text).replace("`", "`\u200b")
