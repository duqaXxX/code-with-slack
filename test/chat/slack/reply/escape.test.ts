import assert from "node:assert/strict";
import { test } from "node:test";
import {
  markdownEscape,
  mrkdwnEscape,
  shownAsWritten,
} from "../../../../src/chat/slack/reply/escape.ts";

test("mrkdwn reads ampersand and angle brackets as markup, so they are escaped", () => {
  assert.equal(mrkdwnEscape("a & <b> c"), "a &amp; &lt;b&gt; c");
  // The ampersand goes first: the entities it writes are not escaped again.
  assert.equal(mrkdwnEscape("&lt;"), "&amp;lt;");
  assert.equal(mrkdwnEscape("plain *text* `code`"), "plain *text* `code`");
});

test("a markdown block's inline characters are each escaped with a backslash", () => {
  assert.equal(markdownEscape("a*b_c`d~e"), "a\\*b\\_c\\`d\\~e");
  assert.equal(markdownEscape("[x](y) {z} & \\"), "\\[x\\]\\(y\\) \\{z\\} \\& \\\\");
  assert.equal(markdownEscape("# + - . !"), "# + - . !");
});

test("model text is shown as written: no link can hide its target and no fence can close", () => {
  assert.equal(shownAsWritten("<https://example.com|safe>"), "&lt;https://example.com|safe&gt;");
  assert.equal(shownAsWritten("```"), "`​`​`​");
});
