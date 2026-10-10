import assert from "node:assert/strict";
import { test } from "node:test";
import { HOLD_CANCEL, HOLD_CONTINUE, holdBlocks } from "../../../src/chat/slack/hold.ts";

type Button = { action_id: string; value: string; style?: string; text: { text: string } };

function buttons(blocks: Record<string, unknown>[]): Button[] {
  const actions = blocks[1] as { elements: Button[] };
  return actions.elements;
}

test("the hold question names the link and carries the id on both buttons", () => {
  const blocks = holdBlocks("hold-1", "<https://example.slack.com/x|Session>");
  assert.deepEqual(blocks[0], {
    type: "section",
    text: {
      type: "mrkdwn",
      text: "Another session is working in this folder: <https://example.slack.com/x|Session>. Send anyway?",
    },
  });
  const [send, keep] = buttons(blocks) as [Button, Button];
  assert.deepEqual([send.action_id, keep.action_id], [HOLD_CONTINUE, HOLD_CANCEL]);
  assert.deepEqual([send.value, keep.value], ["hold-1", "hold-1"]);
});

// Python: `test_the_hold_question_s_buttons_answer_it_in_its_own_words` (tests/test_slack_app.py).
test("the hold question s buttons answer it in its own words", () => {
  // Issue #76: `Continue` beside the shorter `Cancel` made the second look the lesser choice,
  // and a button's width is its text. One `primary` in the set, as the button reference asks.
  const [send, keep] = buttons(holdBlocks("hold-1", "<https://example.slack.com/x|Session>")) as [
    Button,
    Button,
  ];
  assert.deepEqual([send.text.text, send.style], ["Send anyway", "primary"]);
  assert.deepEqual([keep.text.text, keep.style], ["Don't send", undefined]);
  assert.ok(Math.abs(Array.from(send.text.text).length - Array.from(keep.text.text).length) <= 1);
});
