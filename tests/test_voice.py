"""The shapes read here were measured on 2026-10-09 (Slack free plan, slack-sdk 3.45.0) on three
clips recorded in the iOS app: the file object of `conversations.history` and `files.info`, and
the WebVTT body of the file's `vtt` URL."""

from typing import Any

import pytest

from awaydesk import voice

CLIP: dict[str, Any] = {
    "id": "F000CLIP",
    "mimetype": "audio/mp4",
    "subtype": "slack_audio",
    "media_display_type": "audio",
    "transcription": {
        "status": "complete",
        "locale": "en-US",
        "preview": {"content": "What day is it today?", "has_more": False},
    },
}
# The body Slack served for a 15 s clip: a byte order mark, the header with a trailing space,
# cues of a timing line and text, the first text line opening with a dash.
VTT = (
    "﻿WEBVTT \n\n00:00:05.488 --> 00:00:13.349\n- This is an audio clip sent from the"
    "\n\n00:00:13.349 --> 00:00:14.210\nphone today.\n\n\n"
)


def test_a_message_with_one_audio_clip_is_a_clip() -> None:
    assert voice.clip([CLIP]) is CLIP
    # The same recording uploaded as a file: no `subtype`, the same player and transcript.
    uploaded = {k: v for k, v in CLIP.items() if k != "subtype"}
    assert voice.clip([uploaded]) is uploaded


@pytest.mark.parametrize(
    "files",
    [
        [],
        [{**CLIP, "subtype": "slack_video", "media_display_type": "video"}],  # not measured
        [{"id": "F000FILE", "mimetype": "text/plain"}],
        [CLIP, {"id": "F000FILE", "mimetype": "text/plain"}],
    ],
)
def test_anything_else_is_no_clip(files: list[dict[str, Any]]) -> None:
    assert voice.clip(files) is None


@pytest.mark.parametrize(
    "transcription",
    [None, {}, {"status": "processing"}, {"status": "failed"}, "complete"],
)
def test_only_a_complete_transcript_is_ready(transcription: Any) -> None:
    assert voice.ready(CLIP)
    assert not voice.ready({**CLIP, "transcription": transcription})


def test_the_preview_is_the_text_when_slack_did_not_cut_it() -> None:
    assert voice.preview(CLIP) == "What day is it today?"
    cut = {
        **CLIP,
        "transcription": {"status": "complete", "preview": {"content": "What", "has_more": True}},
    }
    assert voice.preview(cut) is None
    assert voice.preview({**CLIP, "transcription": {"status": "complete"}}) is None


def test_the_words_of_a_webvtt_transcript_are_one_line() -> None:
    assert voice.vtt_text(VTT) == "This is an audio clip sent from the phone today."
    assert voice.vtt_text("WEBVTT\n\n") == ""
    # Not seen from Slack, allowed by WebVTT: a cue id, a note, a voice tag.
    tagged = (
        "WEBVTT\n\nNOTE made by a test\n\n1\n00:00:00.000 --> 00:00:01.000\n"
        "<v Alice>Hello</v> <i>there</i>\n"
    )
    assert voice.vtt_text(tagged) == "Hello there"
    assert voice.vtt_text(VTT.replace("\n", "\r\n")) == voice.vtt_text(VTT)
