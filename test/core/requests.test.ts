import assert from "node:assert/strict";
import { test } from "node:test";
import { permissionResult, questionResult } from "../../src/agent/claude/requests.ts";
import type { PermissionAnswer, Question, QuestionAnswer } from "../../src/agent/seam.ts";
import { APPROVE, Approvals, answerOf, DENY, toAnswer } from "../../src/core/requests.ts";
import { DENY_MESSAGE, SKIP_MESSAGE } from "../../src/core/texts.ts";
import { CHANNEL, OTHER_CHANNEL, OTHER_THREAD, THREAD } from "../support/fake-slack.ts";

const TWO: readonly Question[] = [
  {
    text: "Colour?",
    header: "Colour",
    options: [
      { label: "red", description: "Warm", preview: null },
      { label: "blue", description: null, preview: null },
    ],
    multiSelect: false,
  },
  {
    text: "Sizes?",
    header: "Sizes",
    options: [
      { label: "s", description: null, preview: null },
      { label: "l", description: null, preview: null },
    ],
    multiSelect: true,
  },
];

test("resolve only in the same channel and thread and only once", async () => {
  const approvals = new Approvals();
  const [approvalId, pending] = approvals.open(CHANNEL, THREAD, "Bash: ls");
  assert.equal(approvals.resolve(approvalId, OTHER_CHANNEL, THREAD, APPROVE), null);
  assert.equal(approvals.resolve(approvalId, CHANNEL, OTHER_THREAD, APPROVE), null);
  assert.equal(approvals.resolve(approvalId, CHANNEL, THREAD, APPROVE), pending);
  assert.deepEqual(await pending.decision, { allow: true });
  assert.equal(approvals.resolve(approvalId, CHANNEL, THREAD, DENY), null);
});

test("deny all releases every pending request of the same thread", async () => {
  const approvals = new Approvals();
  const [, a] = approvals.open(CHANNEL, THREAD, "a");
  const [, b] = approvals.open(CHANNEL, THREAD, "b");
  const [, otherThread] = approvals.open(CHANNEL, OTHER_THREAD, "c");
  const [, otherChannel] = approvals.open(OTHER_CHANNEL, THREAD, "d");
  const [, question] = approvals.open(CHANNEL, THREAD, "q", TWO);
  const denied = approvals.denyAll(CHANNEL, THREAD);
  assert.deepEqual(new Set(denied.map((p) => p.title)), new Set(["a", "b", "q"]));
  assert.deepEqual(await a.decision, { allow: false, message: DENY_MESSAGE });
  assert.deepEqual(await b.decision, { allow: false, message: DENY_MESSAGE });
  // A question is told it was dismissed, as the Skip button tells it.
  assert.deepEqual(await question.decision, { answered: false, message: SKIP_MESSAGE });
  assert.equal(otherThread.decided, false);
  assert.equal(otherChannel.decided, false);
});

test("pending in reads a thread s requests without resolving them", () => {
  const approvals = new Approvals();
  const [aId, a] = approvals.open(CHANNEL, THREAD, "a");
  const [, otherThread] = approvals.open(CHANNEL, OTHER_THREAD, "c");
  approvals.posted(aId, "ts-a");
  const seen = approvals.pendingIn(CHANNEL, THREAD);
  assert.deepEqual(new Set(seen.map((p) => [p.title, p.messageTs].join("|"))), new Set(["a|ts-a"]));
  assert.equal(a.decided, false);
  assert.equal(otherThread.decided, false);
  // A no-op read: the same request is still there, and still answerable, afterward.
  assert.deepEqual(
    new Set(approvals.pendingIn(CHANNEL, THREAD).map((p) => p.title)),
    new Set(["a"]),
  );
  assert.equal(approvals.resolve(aId, CHANNEL, THREAD, APPROVE), a);
});

test("ids are unguessable and unique", () => {
  const approvals = new Approvals();
  const ids = new Set(Array.from({ length: 100 }, () => approvals.open(CHANNEL, THREAD, "t")[0]));
  assert.equal(ids.size, 100);
  assert.ok([...ids].every((id) => id.length >= 16));
});

test("an answer is the labels picked and the text typed under other", () => {
  const [colour, sizes] = TWO as [Question, Question];
  assert.equal(answerOf(colour, [], "purple"), "purple");
  assert.deepEqual(answerOf(sizes, [0], "xl"), ["s", "xl"]);
  assert.equal(answerOf(colour, [1], undefined), "blue");
  assert.deepEqual(answerOf(sizes, [1, 0], undefined), ["l", "s"]);
  // Typed text is the answer itself on a single choice, even beside a pick.
  assert.equal(answerOf(colour, [0], "purple"), "purple");
  assert.deepEqual(answerOf(sizes, [], "xl"), ["xl"]);
});

test("a question with neither a pick nor a typed text has no answer", () => {
  const [colour, sizes] = TWO as [Question, Question];
  assert.equal(answerOf(colour, [], undefined), null);
  assert.equal(answerOf(sizes, [], ""), null);
});

test("a pick that names no option is refused", () => {
  const [colour] = TWO as [Question];
  assert.throws(() => answerOf(colour, [2], undefined), RangeError);
});

test("to permission matches the documented shapes", () => {
  const toolInput = { command: "ls" };
  const allow: PermissionAnswer = toAnswer(APPROVE, null) as PermissionAnswer;
  assert.deepEqual(permissionResult(allow, toolInput), {
    behavior: "allow",
    updatedInput: toolInput,
  });
  assert.deepEqual(toAnswer(DENY, null), { allow: false, message: DENY_MESSAGE });
  assert.equal(
    permissionResult(toAnswer(DENY, null) as PermissionAnswer, toolInput).behavior,
    "deny",
  );
  const input = { questions: TWO };
  const answered = toAnswer({ kind: "answer", answers: { Q: "A" } }, TWO) as QuestionAnswer;
  assert.deepEqual(questionResult(answered, input), {
    behavior: "allow",
    updatedInput: { questions: TWO, answers: { Q: "A" } },
  });
  // Skip on a question names the dismissal, not the denial.
  assert.deepEqual(toAnswer(DENY, TWO), { answered: false, message: SKIP_MESSAGE });
});

test("a request decided before its message is known says so", () => {
  const approvals = new Approvals();
  const [approvalId] = approvals.open(CHANNEL, THREAD, "Bash: ls");
  approvals.denyAll(CHANNEL, THREAD); // !stop while the request was being posted
  assert.equal(approvals.posted(approvalId, "1790000000.000001"), false);
  const [liveId, pending] = approvals.open(CHANNEL, THREAD, "Bash: ls");
  assert.equal(approvals.posted(liveId, "1790000000.000002"), true);
  assert.equal(pending.messageTs, "1790000000.000002");
});

// The bookkeeping the Python tests reached through the dict.

test("a discarded request is gone", () => {
  const approvals = new Approvals();
  const [approvalId, pending] = approvals.open(CHANNEL, THREAD, "a");
  assert.equal(approvals.get(approvalId), pending);
  approvals.discard(approvalId);
  assert.equal(approvals.get(approvalId), null);
  assert.equal(approvals.resolve(approvalId, CHANNEL, THREAD, APPROVE), null);
});

test("a resolved request is removed and its answer is the seam s", async () => {
  const approvals = new Approvals();
  const [approvalId, pending] = approvals.open(CHANNEL, THREAD, "q", TWO);
  approvals.resolve(approvalId, CHANNEL, THREAD, { kind: "answer", answers: { "Colour?": "red" } });
  assert.equal(approvals.get(approvalId), null);
  assert.equal(pending.decided, true);
  assert.deepEqual(await pending.decision, { answered: true, answers: { "Colour?": "red" } });
});
