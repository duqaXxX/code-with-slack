/**
 * The shapes read here were measured on 2026-10-09 (Slack free plan, slack-sdk 3.45.0) on three
 * clips recorded in the iOS app: the file object of `conversations.history` and `files.info`, and
 * the WebVTT body of the file's `vtt` URL.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import * as voice from "../../../src/chat/slack/voice.ts";

const CLIP: Record<string, unknown> = {
  id: "F000CLIP",
  mimetype: "audio/mp4",
  subtype: "slack_audio",
  media_display_type: "audio",
  transcription: {
    status: "complete",
    locale: "en-US",
    preview: { content: "What day is it today?", has_more: false },
  },
};
// The body Slack served for a 15 s clip: a byte order mark, the header with a trailing space,
// cues of a timing line and text, the first text line opening with a dash.
const VTT =
  "﻿WEBVTT \n\n00:00:05.488 --> 00:00:13.349\n- This is an audio clip sent from the" +
  "\n\n00:00:13.349 --> 00:00:14.210\nphone today.\n\n\n";

test("a message with one audio clip is a clip", () => {
  assert.equal(voice.clip([CLIP]), CLIP);
  // The same recording uploaded as a file: no `subtype`, the same player and transcript.
  const { subtype: _subtype, ...uploaded } = CLIP;
  assert.equal(voice.clip([uploaded]), uploaded);
});

const NOT_CLIPS: [string, Record<string, unknown>[]][] = [
  ["files0", []],
  // not measured
  ["files1", [{ ...CLIP, subtype: "slack_video", media_display_type: "video" }]],
  ["files2", [{ id: "F000FILE", mimetype: "text/plain" }]],
  ["files3", [CLIP, { id: "F000FILE", mimetype: "text/plain" }]],
];
for (const [name, files] of NOT_CLIPS) {
  test(`anything else is no clip [${name}]`, () => {
    assert.equal(voice.clip(files), null);
  });
}

const TRANSCRIPTIONS: [string, unknown][] = [
  ["None", null],
  ["transcription1", {}],
  ["transcription2", { status: "processing" }],
  ["transcription3", { status: "failed" }],
  ["complete", "complete"],
];
for (const [name, transcription] of TRANSCRIPTIONS) {
  test(`only a complete transcript is ready [${name}]`, () => {
    assert.ok(voice.ready(CLIP));
    assert.ok(!voice.ready({ ...CLIP, transcription }));
  });
}

test("the preview is the text when slack did not cut it", () => {
  assert.equal(voice.preview(CLIP), "What day is it today?");
  const cut = {
    ...CLIP,
    transcription: { status: "complete", preview: { content: "What", has_more: true } },
  };
  assert.equal(voice.preview(cut), null);
  assert.equal(voice.preview({ ...CLIP, transcription: { status: "complete" } }), null);
});

test("the words of a webvtt transcript are one line", () => {
  assert.equal(voice.vttText(VTT), "This is an audio clip sent from the phone today.");
  assert.equal(voice.vttText("WEBVTT\n\n"), "");
  // Not seen from Slack, allowed by WebVTT: a cue id, a note, a voice tag.
  const tagged =
    "WEBVTT\n\nNOTE made by a test\n\n1\n00:00:00.000 --> 00:00:01.000\n" +
    "<v Alice>Hello</v> <i>there</i>\n";
  assert.equal(voice.vttText(tagged), "Hello there");
  assert.equal(voice.vttText(VTT.replaceAll("\n", "\r\n")), voice.vttText(VTT));
});
