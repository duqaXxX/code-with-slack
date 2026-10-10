/**
 * Third-party text shown as written in Slack's two markup languages. A leaf module, so the
 * footer, the sinks, the approvals and the lists can all use it without an import cycle.
 */

// The characters that start inline formatting in a markdown block, each escapable with a backslash
// (Slack's markdown block reference, read 2026-09-25). `#`, `+`, `-`, `.` and `!` act only at the
// start of a line. `~` is not in the reference's list, but `~~` renders as strikethrough (measured
// live 2026-09-25); `<` stays text there.
const MARKDOWN_INLINE = /([\\`*_{}[\]()&~])/g;

/** Slack's mrkdwn reads `&`, `<` and `>` as markup; a tool's title is shown as written. */
export function mrkdwnEscape(text: string): string {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

/** Text for inside a markdown block's own formatting, shown as written. */
export function markdownEscape(text: string): string {
  return text.replace(MARKDOWN_INLINE, "\\$1");
}

/**
 * Model-written text for mrkdwn, shown as written: `&`, `<` and `>` escaped, so no
 * `<url|label>` can hide what it links, and a zero-width space after each backtick, so no
 * run of three can close a code block.
 */
export function shownAsWritten(text: string): string {
  return mrkdwnEscape(text).replaceAll("`", "`​");
}
