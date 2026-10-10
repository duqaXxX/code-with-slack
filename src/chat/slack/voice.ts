/**
 * An audio clip as a prompt: the transcript Slack makes of it, sent as the owner's text.
 *
 * Claude Code takes no audio (its Read tool refuses a binary file, measured 2026-10-09, CLI
 * 2.1.292), and its own dictation is the microphone of an interactive session. So a clip becomes a
 * prompt the way dictation fills the terminal's input: as text, before the session sees it. The
 * daemon never transcribes: the text is the one Slack makes when the owner asks for it with
 * `Generate transcript` on the clip (Slack help, "Record audio and video clips", read 2026-10-09).
 *
 * Measured on 2026-10-09 (Slack free plan, slack-sdk 3.45.0), three clips recorded in the iOS app:
 *
 * - a clip is a message with no text and one file whose `media_display_type` is `audio`
 *   (`mimetype` `audio/mp4`): a clip recorded in Slack has the `subtype` `slack_audio`, and the
 *   same recording uploaded as a file has none and gets a transcript all the same;
 * - nothing tells the app when the clip is sent; while Slack makes the transcript it sends
 *   `file_change` for the file, four times in 15 seconds, and the last leaves
 *   `transcription.status` at `complete`. Only `complete` was seen as a status;
 * - `transcription.preview.content` is the text and `has_more` was false, up to 67 characters;
 *   `vtt` is the whole transcript as WebVTT on Slack's file host, read with the bot token;
 * - Slack picks the language itself (`transcription.locale`): of three clips spoken in Italian it
 *   heard one as `it-IT` and two as `en-GB`, and the text of those two was wrong.
 */

// How long a clip waits for the owner to ask Slack for its transcript.
export const WAIT_SECONDS = 300.0;
// A transcript of five minutes of speech, the longest clip Slack records, is a few kilobytes.
export const VTT_LIMIT = 256 * 1024;

// What Python's `str.strip` removes: `str.isspace`, which is not JavaScript's `trim` (it keeps
// U+FEFF out and takes the separators U+001C to U+001F and U+0085 in).
const SPACE =
  "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const STRIP = new RegExp(`^[${SPACE}]+|[${SPACE}]+$`, "g");

function strip(text: string): string {
  return text.replace(STRIP, "");
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/**
 * The audio a message is, when it carries that one file and nothing else: a clip recorded in
 * Slack or an audio file, which Slack shows with the same player and transcript.
 */
export function clip<T extends object>(files: readonly T[]): T | null {
  const [only] = files;
  if (files.length === 1 && only && record(only)?.media_display_type === "audio") return only;
  return null;
}

/** Whether Slack has finished the transcript of `file`. */
export function ready(file: object): boolean {
  return record(record(file)?.transcription)?.status === "complete";
}

/**
 * The transcript when the file object holds all of it; null when Slack cut it (`has_more`) and
 * the rest is in the file's `vtt`.
 */
export function preview(file: object): string | null {
  const found = record(record(record(file)?.transcription)?.preview);
  if (found === null || found.has_more) return null;
  return typeof found.content === "string" ? strip(found.content) : null;
}

/**
 * The words of a WebVTT transcript as Slack writes it: a `WEBVTT` header, then cues of a timing
 * line and text lines, the first of which can open with `- `. One line of text, without the tags
 * WebVTT allows inside a cue (`<v Name>`, `<i>`), which Slack was not seen to write.
 */
export function vttText(body: string): string {
  const words: string[] = [];
  for (const block of body.replace(/^﻿+/, "").replaceAll("\r\n", "\n").split("\n\n")) {
    const lines = block
      .split("\n")
      .map(strip)
      .filter((line) => line !== "");
    const timing = lines.findIndex((line) => line.includes("-->"));
    if (timing === -1) continue; // the header, or a note
    for (const line of lines.slice(timing + 1)) {
      const text = line.replace(/<[^>]*>/g, "");
      words.push(strip(text.startsWith("- ") ? text.slice(2) : text));
    }
  }
  return words.filter((word) => word !== "").join(" ");
}
