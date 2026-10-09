"""An audio clip as a prompt: the transcript Slack makes of it, sent as the owner's text.

Claude Code takes no audio (its Read tool refuses a binary file, measured 2026-10-09, CLI
2.1.292), and its own dictation is the microphone of an interactive session. So a clip becomes a
prompt the way dictation fills the terminal's input: as text, before the session sees it. The
daemon never transcribes: the text is the one Slack makes when the owner asks for it with
`Generate transcript` on the clip (Slack help, "Record audio and video clips", read 2026-10-09).

Measured on 2026-10-09 (Slack free plan, slack-sdk 3.45.0), three clips recorded in the iOS app:

- a clip is a message with no text and one file whose `subtype` is `slack_audio` (`mimetype`
  `audio/mp4`, `media_display_type` `audio`);
- nothing tells the app when the clip is sent; while Slack makes the transcript it sends
  `file_change` for the file, four times in 15 seconds, and the last leaves
  `transcription.status` at `complete`. Only `complete` was seen as a status;
- `transcription.preview.content` is the text and `has_more` was false, up to 67 characters;
  `vtt` is the whole transcript as WebVTT on Slack's file host, read with the bot token;
- Slack picks the language itself (`transcription.locale`): of three clips spoken in Italian it
  heard one as `it-IT` and two as `en-GB`, and the text of those two was wrong.
"""

from typing import Any

# How long a clip waits for the owner to ask Slack for its transcript.
WAIT_SECONDS = 300.0
# A transcript of five minutes of speech, the longest clip Slack records, is a few kilobytes.
VTT_LIMIT = 256 * 1024


def clip(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The audio clip a message is, when it carries that one file and nothing else."""
    if len(files) == 1 and files[0].get("subtype") == "slack_audio":
        return files[0]
    return None


def ready(file: dict[str, Any]) -> bool:
    """Whether Slack has finished the transcript of `file`."""
    transcription = file.get("transcription")
    return isinstance(transcription, dict) and transcription.get("status") == "complete"


def preview(file: dict[str, Any]) -> str | None:
    """The transcript when the file object holds all of it; None when Slack cut it (`has_more`)
    and the rest is in the file's `vtt`."""
    found = (file.get("transcription") or {}).get("preview")
    if not isinstance(found, dict) or found.get("has_more"):
        return None
    content = found.get("content")
    return content.strip() if isinstance(content, str) else None


def vtt_text(body: str) -> str:
    """The words of a WebVTT transcript as Slack writes it: a `WEBVTT` header, then cues of a
    timing line and text lines, the first of which can open with `- `. One line of text."""
    words: list[str] = []
    for block in body.lstrip("﻿").replace("\r\n", "\n").split("\n\n"):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not any("-->" in line for line in lines):
            continue  # the header, or a note
        timing = next(i for i, line in enumerate(lines) if "-->" in line)
        for line in lines[timing + 1 :]:
            words.append(line.removeprefix("- ").strip())
    return " ".join(word for word in words if word)
