/**
 * The Claude back end's wire records into session events, on the recorded streams. The expected
 * values are read off the recordings: `tests/fixtures/sdk/*.jsonl`, Claude Code 2.1.286, with
 * `compact`, `auto-compact` and `auto-compact-report-turn` on 2.1.292. The shapes no recording
 * holds (a server tool's blocks, a stream that names no message) are the ones the Python SDK's
 * parser and the Python renderer read, claude-agent-sdk 0.2.165.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  fileChange,
  kindOf,
  TRANSLATED_KINDS,
  Translator,
  UNREAD_KINDS,
} from "../../../src/agent/claude/translate.ts";
import type { SessionEvent, SessionEventType } from "../../../src/agent/seam.ts";
import { type JsonObject, sdkRecordings, sdkRecords } from "../../support/fixtures.ts";
import {
  recordedEvents as events,
  eventsOf as of,
  splitTurns as turns,
} from "../../support/replay.ts";

/** The events of each record of a recording, by one translator that was told `sent`. */
function perRecord(name: string, sent: readonly string[] = []): SessionEvent[][] {
  const translator = new Translator();
  for (const promptId of sent) translator.promptSent(promptId);
  return sdkRecords(name).map((record) => translator.translate(record));
}

function types(all: readonly SessionEvent[]): SessionEventType[] {
  return all.map((event) => event.type);
}

/** The record at `index` of a recording, which must exist. */
function record(name: string, index: number): JsonObject {
  const found = sdkRecords(name)[index];
  assert.ok(found, `${name} has no record ${index}`);
  return found;
}

const PROJECT = "/home/dev/project";
const HAIKU = "claude-haiku-4-5-20251001";

// Every recording, every kind.

test("every recording translates without throwing", () => {
  for (const name of sdkRecordings()) {
    assert.doesNotThrow(() => events(name), name);
  }
});

test("every recorded kind is translated or named as one the daemon does not read", () => {
  const known = new Set<string>([...TRANSLATED_KINDS, ...UNREAD_KINDS]);
  const unknown = new Set<string>();
  for (const name of sdkRecordings()) {
    for (const wire of sdkRecords(name)) {
      const kind = kindOf(wire);
      if (kind === null || !known.has(kind)) unknown.add(`${name}: ${kind}`);
    }
  }
  assert.deepEqual([...unknown], []);
});

test("no kind is both translated and unread", () => {
  const unread = new Set<string>(UNREAD_KINDS);
  assert.deepEqual(
    TRANSLATED_KINDS.filter((kind) => unread.has(kind)),
    [],
  );
});

test("every translated kind gives an event in some recording", () => {
  const silent = new Set<string>(TRANSLATED_KINDS);
  for (const name of sdkRecordings()) {
    const wire = sdkRecords(name);
    perRecord(name).forEach((made, index) => {
      if (made.length > 0) silent.delete(kindOf(wire[index]) ?? "");
    });
  }
  assert.deepEqual([...silent], []);
});

test("every kind named as unread is in some recording and gives no event", () => {
  const unseen = new Set<string>(UNREAD_KINDS);
  for (const name of sdkRecordings()) {
    const wire = sdkRecords(name);
    perRecord(name).forEach((made, index) => {
      const kind = kindOf(wire[index]) ?? "";
      if (!unseen.has(kind) && !UNREAD_KINDS.includes(kind)) return;
      unseen.delete(kind);
      assert.deepEqual(made, [], `${name} record ${index}, ${kind}`);
    });
  }
  assert.deepEqual([...unseen], []);
});

test("a record of a kind nobody knows gives no event", () => {
  const translator = new Translator();
  assert.deepEqual(translator.translate({ type: "hologram", session_id: "s" }), []);
  assert.deepEqual(translator.translate({ type: "system", subtype: "hologram" }), []);
  assert.deepEqual(translator.translate({ type: "stream_event", event: { type: "hologram" } }), []);
});

test("what is not a record gives no event", () => {
  const translator = new Translator();
  for (const value of [null, undefined, "text", 7, [], [{ type: "result" }], {}, { type: 7 }]) {
    assert.deepEqual(translator.translate(value), []);
    assert.equal(kindOf(value), null);
  }
});

test("a kind is the type, with the subtype of a system record and the event of a stream event", () => {
  assert.equal(kindOf(record("clear", 0)), "system:init");
  assert.equal(kindOf(record("clear", 2)), "stream_event:message_start");
  assert.equal(kindOf(record("clear", 11)), "assistant");
  assert.equal(kindOf(record("clear", 15)), "result");
  assert.equal(kindOf(record("clear", 16)), "conversation_reset");
});

// The session and its turns.

test("an init names the session and the agent's version", () => {
  assert.deepEqual(perRecord("clear")[0], [
    {
      type: "session_started",
      sessionId: "a06ebfdf-566b-468a-bbc0-1bab7996651a",
      agentVersion: "2.1.286",
    },
  ]);
});

test("a cleared conversation goes on under a new session id", () => {
  const all = events("clear");
  const before = "a06ebfdf-566b-468a-bbc0-1bab7996651a";
  const after = "ada657bf-b140-4863-944c-dbad24b5dc14";
  assert.deepEqual(
    of(all, "session_started").map((event) => event.sessionId),
    [before, after],
  );
  assert.deepEqual(
    of(all, "turn_ended").map((event) => event.sessionId),
    [before, after],
  );
});

test("a turn's end carries its last text, who started it and the tokens of each model", () => {
  assert.deepEqual(of(events("tools"), "turn_ended"), [
    {
      type: "turn_ended",
      sessionId: "cce8a58a-5d75-487b-a79a-2f38a183fea7",
      startedBy: "owner",
      finalText: 'The notes.txt file contains two entries: "alpha" and "beta".',
      ending: "done",
      steps: 3,
      tokens: {
        [HAIKU]: { input: 936, output: 328, cacheRead: 26480, cacheCreation: 4645 },
      },
    },
  ]);
});

test("a turn that ran no model has no tokens", () => {
  const [first, second] = of(events("clear"), "turn_ended");
  assert.deepEqual(Object.keys(first?.tokens ?? {}), [HAIKU]);
  assert.deepEqual(second?.tokens, {});
  assert.equal(second?.finalText, "");
});

test("a turn the agent starts to report a task says so", () => {
  assert.deepEqual(
    of(events("background"), "turn_ended").map((event) => event.startedBy),
    ["owner", "agent"],
  );
});

test("an interrupted turn ends as interrupted, with no last text", () => {
  const [ended] = of(events("interrupt"), "turn_ended");
  assert.equal(ended?.ending, "interrupted");
  assert.equal(ended?.finalText, null);
  assert.equal(ended?.startedBy, "owner");
});

test("an interrupted report turn is still the agent's", () => {
  const ended = of(events("prompt-replay-stop-queued"), "turn_ended");
  assert.deepEqual(
    ended.map((event) => [event.startedBy, event.ending]),
    [
      ["owner", "done"],
      ["agent", "interrupted"],
      ["owner", "done"],
      ["owner", "done"],
    ],
  );
});

test("a turn the agent reports as failed ends as an error", () => {
  const [ended] = of(events("auth-failed"), "turn_ended");
  assert.equal(ended?.ending, "error");
  assert.equal(ended?.finalText, "Not logged in · Please run /login");
});

test("an origin of the owner's own is the owner's turn", () => {
  // `human` is the Python package's `MessageOriginKind` for the owner's prompt (0.2.165); the
  // recordings carry no origin on such a turn.
  const translator = new Translator();
  const result = record("clear", 15);
  const [ended] = of(translator.translate({ ...result, origin: { kind: "human" } }), "turn_ended");
  assert.equal(ended?.startedBy, "owner");
  const [malformed] = of(translator.translate({ ...result, origin: { kind: 7 } }), "turn_ended");
  assert.equal(malformed?.startedBy, "owner");
});

// Text.

test("a streamed message gives its start, its text as it is written and its end", () => {
  const made = perRecord("clear");
  const id = "msg_011CfaHYNurMQWkVVn2RLXQw";
  assert.deepEqual(made[2], [{ type: "message_started", messageId: id, parentCallId: null }]);
  assert.deepEqual(made[9], [{ type: "text_started", messageId: id, parentCallId: null }]);
  assert.deepEqual(made[10], [
    { type: "text_delta", messageId: id, text: "ok", parentCallId: null },
  ]);
  assert.deepEqual(made[14], [{ type: "message_ended", messageId: id, parentCallId: null }]);
  // Thinking, the signature of a block and a block's end show nothing.
  for (const index of [3, 5, 6, 7, 8, 12, 13]) assert.deepEqual(made[index], [], `record ${index}`);
});

test("the message that repeats a streamed text gives no text", () => {
  const made = perRecord("clear");
  assert.equal(record("clear", 11).type, "assistant");
  assert.deepEqual(made[11], []);
});

test("a message the stream announced gives no text once its stream has ended either", () => {
  // No recording has this order: the record always comes while its stream is open. The id is
  // what tells then.
  const translator = new Translator();
  translator.translate(record("clear", 2));
  translator.translate(record("clear", 10));
  translator.translate(record("clear", 14));
  assert.deepEqual(translator.translate(record("clear", 11)), []);
});

test("a text no stream announced arrives whole", () => {
  const made = perRecord("usage");
  assert.equal(made[1]?.length, 1);
  const [whole] = of(made[1] ?? [], "text");
  assert.equal(whole?.messageId, "05598563-bf5a-470b-a6b9-17e5aa13b82d");
  assert.equal(whole?.parentCallId, null);
  assert.ok(whole?.text.startsWith("You are currently using your subscription"));
  assert.equal(whole?.text, whole?.text.trim());
});

test("a command's own output comes whole before the streamed text of its turn", () => {
  const all = events("goal");
  const written = all.filter((event) => event.type === "text" || event.type === "text_delta");
  assert.deepEqual(written[0], {
    type: "text",
    messageId: "f253d2e7-39dc-4042-9737-175c37c817d3",
    text: "Goal set: Reply with the single word tick once in each turn, until you have said it three times in total. Never use a tool.",
    parentCallId: null,
  });
  assert.deepEqual(
    written.slice(1).map((event) => (event.type === "text_delta" ? event.text : null)),
    [
      "Acknowledge",
      "d. I'll rep",
      'ly with "ti',
      'ck" once pe',
      "r turn unti",
      "l I've said",
      " it three t",
      "imes.\n\ntick",
      "tick",
      "tick",
    ],
  );
});

test("each text of a streamed message is announced before its first piece", () => {
  // Three messages in one turn, each with one text: the renderer breaks the paragraph there.
  const kinds = types(events("goal")).filter(
    (type) => type === "text_started" || type === "text_delta" || type === "message_started",
  );
  assert.deepEqual(kinds, [
    "message_started",
    "text_started",
    ...Array<SessionEventType>(8).fill("text_delta"),
    "message_started",
    "text_started",
    "text_delta",
    "message_started",
    "text_started",
    "text_delta",
  ]);
});

test("an empty piece of text is left out", () => {
  const translator = new Translator();
  translator.translate(record("clear", 2));
  const delta = record("clear", 10);
  const empty = { ...delta, event: { type: "content_block_delta", delta: { type: "text_delta" } } };
  assert.deepEqual(translator.translate(empty), []);
});

test("a whole text is held back while a streamed message is unfinished", () => {
  // Its text may be the stream's, sent again whole after a failed stream.
  const translator = new Translator();
  translator.translate(record("clear", 2));
  assert.deepEqual(translator.translate(record("usage", 1)), []);
  translator.translate(record("clear", 14));
  assert.equal(translator.translate(record("usage", 1)).length, 1);
});

test("a whole text is held back once a stream named no message", () => {
  const translator = new Translator();
  const start = record("clear", 2);
  const unnamed = { ...start, event: { type: "message_start", message: {} } };
  assert.deepEqual(translator.translate(unnamed), [
    { type: "message_started", messageId: null, parentCallId: null },
  ]);
  translator.translate(record("clear", 14));
  assert.deepEqual(translator.translate(record("usage", 1)), []);
});

test("a turn's end forgets the stream it leaves unfinished", () => {
  // An interrupted stream has no stop: the next turn's command output must still show.
  const translator = new Translator();
  translator.translate(record("clear", 2));
  translator.translate(record("clear", 15));
  assert.equal(translator.translate(record("usage", 1)).length, 1);
});

test("a message with no words gives no text", () => {
  const translator = new Translator();
  const blank = record("usage", 1);
  const message = { ...(blank.message as JsonObject), content: [{ type: "text", text: " \n\t" }] };
  assert.deepEqual(translator.translate({ ...blank, message }), []);
});

test("a subagent's text arrives whole under the call that runs it", () => {
  const made = perRecord("subagent");
  assert.deepEqual(made[76], [
    {
      type: "text",
      messageId: "msg_011CfaHEeQFrp2bisgSqtfX6",
      text: "The current working directory is empty except for a single file:\n\n**Files and directories present:**\n- `notes.txt` (6 bytes, regular file)\n\nThat's the only item in `/home/dev/project`.",
      parentCallId: "toolu_013vuT1tVKbAduiW9vAf1YWk",
    },
  ]);
});

test("a text reaches the core once, streamed or whole and never both", () => {
  for (const name of sdkRecordings()) {
    const wire = sdkRecords(name);
    const made = perRecord(name);
    // Each stretch between two ends of turn: the synthetic recordings reuse a message id.
    let streamed = new Set<string | null>();
    let whole = new Set<string>();
    made.forEach((group, index) => {
      for (const event of group) {
        if (event.type === "text_delta") streamed.add(event.messageId);
        if (event.type === "text") {
          assert.ok(!whole.has(event.messageId), `${name} record ${index}: whole twice`);
          assert.ok(!streamed.has(event.messageId), `${name} record ${index}: streamed and whole`);
          whole.add(event.messageId);
        }
      }
      if (wire[index]?.type === "result") {
        streamed = new Set();
        whole = new Set();
      }
    });
  }
});

test("the streamed pieces of a message spell the text its record repeats", () => {
  for (const name of sdkRecordings()) {
    const wire = sdkRecords(name);
    let pieces = "";
    perRecord(name).forEach((group, index) => {
      for (const event of group) {
        if (event.type === "text_started") pieces = "";
        if (event.type === "text_delta") pieces += event.text;
      }
      const entry = wire[index];
      if (entry?.type !== "assistant" || entry.parent_tool_use_id || "error" in entry) return;
      const [block] = (entry.message as JsonObject).content as JsonObject[];
      // A text record that gave no event is one the stream had already written.
      if (block?.type !== "text" || group.length > 0) return;
      assert.equal(pieces, block.text, `${name} record ${index}`);
    });
  }
});

// Calls.

test("a call starts with its tool's name and its input as the agent sent it", () => {
  const made = perRecord("tools");
  assert.deepEqual(made[19], [
    {
      type: "call_started",
      callId: "toolu_01Xdzy1JrqnyQh4m9GkKaQvo",
      toolName: "Bash",
      input: { command: "ls", description: "List files in current directory" },
      parentCallId: null,
    },
  ]);
  assert.deepEqual(made[53], [
    {
      type: "call_started",
      callId: "toolu_01LnSiTbh8GtkisVrbigeaKe",
      toolName: "Read",
      input: { file_path: `${PROJECT}/notes.txt` },
      parentCallId: null,
    },
  ]);
});

test("a call ends with its result's text", () => {
  const made = perRecord("tools");
  assert.deepEqual(made[24], [
    {
      type: "call_ended",
      callId: "toolu_01Xdzy1JrqnyQh4m9GkKaQvo",
      isError: false,
      output: "notes.txt",
      fileChange: null,
      parentCallId: null,
    },
  ]);
  // A Read's result names a file too, in another shape: it is no change to one.
  assert.deepEqual(made[57], [
    {
      type: "call_ended",
      callId: "toolu_01LnSiTbh8GtkisVrbigeaKe",
      isError: false,
      output: "1\talpha\n2\tbeta\n3\t",
      fileChange: null,
      parentCallId: null,
    },
  ]);
});

test("a failed call ends as an error with what the tool said", () => {
  const made = perRecord("tool-error");
  assert.deepEqual(made[50], [
    {
      type: "call_ended",
      callId: "toolu_01YYyHT3cAveY6vGoBUSEazb",
      isError: true,
      output: `File does not exist. Note: your current working directory is ${PROJECT}.`,
      fileChange: null,
      parentCallId: null,
    },
  ]);
  assert.deepEqual(of(made[65] ?? [], "call_ended")[0]?.output, "Exit code 1");
});

test("a result written in parts ends its call with their texts, a line each", () => {
  const [ended] = of(perRecord("subagent")[31] ?? [], "call_ended");
  assert.equal(ended?.callId, "toolu_013vuT1tVKbAduiW9vAf1YWk");
  assert.equal(ended?.isError, false);
  assert.ok(ended?.output?.startsWith("Async agent launched successfully."));
  // One part: a second would start on a line of its own.
  const translator = new Translator();
  const wire = record("subagent", 31);
  const [block] = (wire.message as JsonObject).content as JsonObject[];
  const content = [
    { type: "text", text: "one" },
    "skipped",
    { type: "tool_reference" },
    { type: "text", text: "two" },
  ];
  const message = { role: "user", content: [{ ...block, content }] };
  assert.equal(
    of(translator.translate({ ...wire, message }), "call_ended")[0]?.output,
    "one\n\ntwo",
  );
});

test("a result with no text ends its call with none", () => {
  const translator = new Translator();
  const wire = record("tools", 24);
  const [block] = (wire.message as JsonObject).content as JsonObject[];
  const without = { tool_use_id: block?.tool_use_id as string, type: "tool_result" };
  const message = { role: "user", content: [without] };
  const [ended] = of(translator.translate({ ...wire, message }), "call_ended");
  assert.equal(ended?.output, null);
  assert.equal(ended?.isError, false);
});

test("a subagent's calls name the call that runs it", () => {
  const made = perRecord("subagent");
  const agent = "toolu_013vuT1tVKbAduiW9vAf1YWk";
  assert.deepEqual(made[72], [
    {
      type: "call_started",
      callId: "toolu_01NJC7PqCRjvqpuDbQxHDXTp",
      toolName: "Bash",
      input: {
        command: "ls -la",
        description: "List files and directories in current working directory",
      },
      parentCallId: agent,
    },
  ]);
  const [ended] = of(made[74] ?? [], "call_ended");
  assert.equal(ended?.callId, "toolu_01NJC7PqCRjvqpuDbQxHDXTp");
  assert.equal(ended?.parentCallId, agent);
  assert.ok(ended?.output?.startsWith("total 8\n"));
});

test("a server tool's call starts and ends like any other", () => {
  // No recording holds one: the blocks are those the Python SDK's parser reads
  // (`server_tool_use`, `advisor_tool_result`), which the Python renderer shows as a call.
  const translator = new Translator();
  const wire = record("tools", 19);
  const base = wire.message as JsonObject;
  const use = { type: "server_tool_use", id: "srvtoolu_000", name: "advisor", input: { q: "why" } };
  assert.deepEqual(translator.translate({ ...wire, message: { ...base, content: [use] } }), [
    {
      type: "call_started",
      callId: "srvtoolu_000",
      toolName: "advisor",
      input: { q: "why" },
      parentCallId: null,
    },
  ]);
  const result = {
    type: "advisor_tool_result",
    tool_use_id: "srvtoolu_000",
    content: { type: "advisor_result", text: "because" },
  };
  assert.deepEqual(translator.translate({ ...wire, message: { ...base, content: [result] } }), [
    {
      type: "call_ended",
      callId: "srvtoolu_000",
      isError: false,
      output: "advisor_result",
      fileChange: null,
      parentCallId: null,
    },
  ]);
});

test("a call's end names a call that started", () => {
  // In every recording: none shows a result whose call the stream never carried.
  for (const name of sdkRecordings()) {
    const started = new Set<string>();
    for (const event of events(name)) {
      if (event.type === "call_started") started.add(event.callId);
      if (event.type === "call_ended")
        assert.ok(started.has(event.callId), `${name}: ${event.callId}`);
    }
  }
});

test("a call starts once", () => {
  for (const name of sdkRecordings()) {
    const started = of(events(name), "call_started").map((event) => event.callId);
    // The synthetic recordings of a subagent's failed request reuse one call id per turn.
    if (name.startsWith("subagent-api-error")) continue;
    assert.equal(new Set(started).size, started.length, name);
  }
});

// File changes.

test("a created file is a change with its content and no hunk", () => {
  const [ended] = of(perRecord("edit-write")[55] ?? [], "call_ended");
  assert.equal(ended?.callId, "toolu_01LNsPBgUNiNWFnk216PQs6q");
  assert.deepEqual(ended?.fileChange, {
    path: `${PROJECT}/new.txt`,
    kind: "created",
    hunks: [],
    content: "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n",
  });
});

test("an edit is a change with its hunks, each line under its sign", () => {
  const [ended] = of(perRecord("edit-write")[175] ?? [], "call_ended");
  assert.equal(ended?.callId, "toolu_01G23oPQ4Pz1p1Srsji7xLzs");
  assert.deepEqual(ended?.fileChange, {
    path: `${PROJECT}/notes.txt`,
    kind: "edited",
    hunks: [{ oldStart: 1, newStart: 1, lines: [" alpha", "-beta", "+gamma", " delta"] }],
    content: null,
  });
});

test("a write over an existing file is an edit", () => {
  const [ended] = of(perRecord("edit-write")[213] ?? [], "call_ended");
  assert.deepEqual(ended?.fileChange, {
    path: `${PROJECT}/notes.txt`,
    kind: "edited",
    hunks: [{ oldStart: 1, newStart: 1, lines: ["-alpha", "-gamma", "-delta", "+one", "+two"] }],
    content: null,
  });
});

test("a failed edit changed no file", () => {
  const [ended] = of(perRecord("edit-write")[137] ?? [], "call_ended");
  assert.equal(ended?.isError, true);
  assert.equal(ended?.fileChange, null);
  assert.equal(
    ended?.output,
    "<tool_use_error>String to replace not found in file.\nString: 2\tbeta</tool_use_error>",
  );
});

test("only the three calls that wrote a file carry a change, in every recording", () => {
  const changed: string[] = [];
  for (const name of sdkRecordings()) {
    for (const event of of(events(name), "call_ended")) {
      if (event.fileChange !== null) changed.push(`${name}: ${event.fileChange.kind}`);
    }
  }
  assert.deepEqual(changed, ["edit-write: created", "edit-write: edited", "edit-write: edited"]);
});

const EDIT = {
  filePath: `${PROJECT}/notes.txt`,
  structuredPatch: [{ oldStart: 3, oldLines: 1, newStart: 3, newLines: 1, lines: ["-a", "+b"] }],
};

test("a result in another shape than the measured one is no file change", () => {
  const [hunk] = EDIT.structuredPatch;
  const shapes: unknown[] = [
    null,
    undefined,
    "Error: Exit code 1",
    [],
    {},
    { ...EDIT, filePath: 7 },
    { structuredPatch: EDIT.structuredPatch },
    { ...EDIT, structuredPatch: [] },
    { ...EDIT, structuredPatch: "patch" },
    { ...EDIT, structuredPatch: ["-a"] },
    { ...EDIT, structuredPatch: [{ ...hunk, oldStart: "3" }] },
    { ...EDIT, structuredPatch: [{ ...hunk, newStart: 3.5 }] },
    { ...EDIT, structuredPatch: [{ ...hunk, lines: "-a" }] },
    { ...EDIT, structuredPatch: [{ ...hunk, lines: ["-a", 7] }] },
    { ...EDIT, structuredPatch: [{ ...hunk, lines: ["-a", ""] }] },
    { ...EDIT, structuredPatch: [{ ...hunk, lines: ["-a", "?b"] }] },
    { ...EDIT, structuredPatch: [hunk, { ...hunk, lines: ["*b"] }] },
    { type: "create", filePath: `${PROJECT}/new.txt`, structuredPatch: [] },
    { type: "create", filePath: `${PROJECT}/new.txt`, content: 7, structuredPatch: [hunk] },
    { type: "text", file: { filePath: `${PROJECT}/notes.txt`, content: "alpha\n" } },
  ];
  for (const shape of shapes) assert.equal(fileChange(shape), null, JSON.stringify(shape));
});

test("a note about the line above is a line of the hunk like the others", () => {
  const lines = ["-a", "\\ No newline at end of file", "+b"];
  const [hunk] = EDIT.structuredPatch;
  assert.deepEqual(fileChange({ ...EDIT, structuredPatch: [{ ...hunk, lines }] }), {
    path: `${PROJECT}/notes.txt`,
    kind: "edited",
    hunks: [{ oldStart: 3, newStart: 3, lines }],
    content: null,
  });
});

test("a created file keeps the hunks its result holds", () => {
  const created = { ...EDIT, type: "create", content: "b\n" };
  assert.deepEqual(fileChange(created), {
    path: `${PROJECT}/notes.txt`,
    kind: "created",
    hunks: [{ oldStart: 3, newStart: 3, lines: ["-a", "+b"] }],
    content: "b\n",
  });
  assert.deepEqual(fileChange({ ...created, structuredPatch: "patch" })?.hunks, []);
});

test("a result shared by several calls is no one's file change", () => {
  // One `tool_use_result` per message: it belongs to a result only when it is alone.
  const translator = new Translator();
  const wire = record("edit-write", 175);
  const [block] = (wire.message as JsonObject).content as JsonObject[];
  const second = { ...block, tool_use_id: "toolu_000" };
  const message = { role: "user", content: [block, second] };
  const ended = of(translator.translate({ ...wire, message }), "call_ended");
  assert.deepEqual(
    ended.map((event) => [event.callId, event.fileChange]),
    [
      ["toolu_01G23oPQ4Pz1p1Srsji7xLzs", null],
      ["toolu_000", null],
    ],
  );
});

// Tasks.

test("a background command is a task of the call that launched it", () => {
  const made = perRecord("background");
  const call = "toolu_01LiZwhcy5g12fYSVLgAq5TS";
  assert.deepEqual(made[28], [
    {
      type: "task_started",
      taskId: "bny2rux7d",
      kind: "command",
      taskType: "local_bash",
      description: "Sleep for 5 seconds then print done",
      callId: call,
    },
  ]);
  assert.deepEqual(made[45], [
    { type: "task_updated", taskId: "bny2rux7d", status: "completed", terminal: true },
  ]);
  assert.deepEqual(made[46], [
    {
      type: "task_ended",
      taskId: "bny2rux7d",
      status: "completed",
      summary: 'Background command "Sleep for 5 seconds then print done" completed (exit code 0)',
      durationMs: null,
      callId: call,
    },
  ]);
});

test("a subagent is a task with its progress and the time it took", () => {
  const made = perRecord("subagent");
  const call = "toolu_013vuT1tVKbAduiW9vAf1YWk";
  const task = "a42694cd437f15ed1";
  assert.deepEqual(made[30], [
    {
      type: "task_started",
      taskId: task,
      kind: "subagent",
      taskType: "local_agent",
      description: "Run ls and report contents",
      callId: call,
    },
  ]);
  assert.deepEqual(made[73], [
    {
      type: "task_progress",
      taskId: task,
      description: "Running List files and directories in current working directory",
      callId: call,
    },
  ]);
  const [ended] = of(made[79] ?? [], "task_ended");
  assert.equal(ended?.status, "completed");
  assert.equal(ended?.durationMs, 4886);
  assert.equal(ended?.callId, call);
  assert.ok(ended?.summary.startsWith("The current working directory is empty"));
});

test("a task no call started names none", () => {
  const made = perRecord("skill-fork-command");
  assert.deepEqual(made[0], [
    {
      type: "task_started",
      taskId: "ac1ab5ef6df9df287",
      kind: "subagent",
      taskType: "local_agent",
      description: "/list-files",
      callId: null,
    },
  ]);
  assert.deepEqual(made[2], [
    {
      type: "task_ended",
      taskId: "ac1ab5ef6df9df287",
      status: "completed",
      summary: "/list-files",
      durationMs: null,
      callId: null,
    },
  ]);
});

test("a stopped task ends as stopped and a failed one as failed", () => {
  const [stopped] = of(events("interrupt"), "task_ended");
  assert.equal(stopped?.status, "stopped");
  assert.equal(stopped?.taskId, "byfb8zlap");
  const failed = events("subagent-api-error-foreground");
  assert.deepEqual(
    of(failed, "task_updated").map((event) => [event.status, event.terminal]),
    [["failed", true]],
  );
  assert.equal(of(failed, "task_ended")[0]?.status, "failed");
  assert.equal(of(failed, "call_ended")[0]?.isError, true);
});

test("a task of a kind the daemon has no word for crosses under the agent's own", () => {
  // `local_workflow` is a `task_type` the TypeScript SDK 0.3.296 names; no recording holds it.
  const translator = new Translator();
  const started = record("background", 28);
  const [other] = of(
    translator.translate({ ...started, task_type: "local_workflow" }),
    "task_started",
  );
  assert.deepEqual([other?.kind, other?.taskType], ["other", "local_workflow"]);
  const { task_type: _, ...untyped } = started;
  const [none] = of(translator.translate(untyped), "task_started");
  assert.deepEqual([none?.kind, none?.taskType], ["other", null]);
});

test("a task's change that is not its end is not terminal", () => {
  // No recording holds one: the patches are those `SDKTaskUpdatedMessage` declares (TypeScript
  // SDK 0.3.296), a `status` of `running` and an `is_backgrounded` with no status.
  const translator = new Translator();
  const updated = record("background", 45);
  const running = { ...updated, patch: { status: "running" } };
  assert.deepEqual(translator.translate(running), [
    { type: "task_updated", taskId: "bny2rux7d", status: "running", terminal: false },
  ]);
  const backgrounded = { ...updated, patch: { is_backgrounded: true } };
  assert.deepEqual(translator.translate(backgrounded), [
    { type: "task_updated", taskId: "bny2rux7d", status: null, terminal: false },
  ]);
  for (const status of ["completed", "failed", "stopped", "killed"]) {
    const [event] = of(translator.translate({ ...updated, patch: { status } }), "task_updated");
    assert.equal(event?.terminal, true, status);
  }
});

test("every change of a task the recordings hold is its end", () => {
  for (const name of sdkRecordings()) {
    for (const event of of(events(name), "task_updated")) {
      assert.equal(event.terminal, true, `${name}: ${event.taskId}`);
    }
  }
});

test("every task that ends had started", () => {
  for (const name of sdkRecordings()) {
    const started = new Set<string>();
    for (const event of events(name)) {
      if (event.type === "task_started") started.add(event.taskId);
      if (event.type === "task_ended" || event.type === "task_updated") {
        assert.ok(started.has(event.taskId), `${name}: ${event.taskId}`);
      }
    }
  }
});

// Compaction, modes, limits.

test("a compaction on request starts, ends and then says what it saved", () => {
  const made = perRecord("compact").slice(73, 77);
  assert.deepEqual(made, [
    [{ type: "compaction_started" }],
    [{ type: "compaction_ended", result: "success" }],
    [
      {
        type: "session_started",
        sessionId: "b40aff10-44ed-4a7b-9d3c-9cac84134264",
        agentVersion: "2.1.292",
      },
    ],
    [{ type: "compacted", tokensBefore: 20742, tokensAfter: 4995 }],
  ]);
});

test("an automatic compaction comes before every frame of its turn that shows something", () => {
  const prompt = "8f957d15-1fb5-4203-be70-76ff210b7d09";
  const all = events("auto-compact", [prompt]);
  assert.deepEqual(types(all).slice(0, 6), [
    "session_started",
    "compaction_started",
    "compaction_ended",
    "prompt_taken",
    "compacted",
    "message_started",
  ]);
  assert.deepEqual(of(all, "compacted"), [
    { type: "compacted", tokensBefore: 67900, tokensAfter: 10598 },
  ]);
});

test("a report turn that compacts first shows the compaction before its words", () => {
  const all = events("auto-compact-report-turn");
  const [, report] = turns(all);
  assert.deepEqual(types(report ?? []).slice(0, 7), [
    "task_updated",
    "task_ended",
    "session_started",
    "compaction_started",
    "compaction_ended",
    "compacted",
    "message_started",
  ]);
  assert.deepEqual(of(all, "compacted"), [
    { type: "compacted", tokensBefore: 68175, tokensAfter: 10689 },
  ]);
});

test("a compaction that gives no counts is still said", () => {
  const translator = new Translator();
  const boundary = record("compact", 76);
  const { compact_metadata: _, ...bare } = boundary;
  assert.deepEqual(translator.translate(bare), [
    { type: "compacted", tokensBefore: null, tokensAfter: null },
  ]);
  const partial = { ...boundary, compact_metadata: { pre_tokens: 20742, post_tokens: "few" } };
  assert.deepEqual(translator.translate(partial), [
    { type: "compacted", tokensBefore: 20742, tokensAfter: null },
  ]);
});

test("a compaction starts once however often the agent says it is compacting", () => {
  const translator = new Translator();
  const compacting = record("compact", 73);
  assert.deepEqual(translator.translate(compacting), [{ type: "compaction_started" }]);
  assert.deepEqual(translator.translate(compacting), []);
  assert.deepEqual(translator.translate(record("compact", 74)), [
    { type: "compaction_ended", result: "success" },
  ]);
  assert.deepEqual(translator.translate(record("compact", 74)), []);
});

test("a permission mode report does not end a compaction", () => {
  const translator = new Translator();
  translator.translate(record("compact", 73));
  assert.deepEqual(translator.translate(record("permission-mode-status", 0)), [
    { type: "mode_changed", mode: "bypassPermissions" },
  ]);
  assert.deepEqual(translator.translate(record("compact", 74)), [
    { type: "compaction_ended", result: "success" },
  ]);
});

test("a status of another kind ends a compaction with no result", () => {
  const translator = new Translator();
  translator.translate(record("compact", 73));
  assert.equal(record("compact", 1).status, "requesting");
  assert.deepEqual(translator.translate(record("compact", 1)), [
    { type: "compaction_ended", result: null },
  ]);
});

test("a turn's end leaves no compaction behind", () => {
  // The core drops its own at the end of the turn: a second end would be one too many.
  const translator = new Translator();
  translator.translate(record("compact", 73));
  assert.deepEqual(types(translator.translate(record("compact", 79))), ["turn_ended"]);
  assert.deepEqual(translator.translate(record("compact", 74)), []);
  assert.deepEqual(translator.translate(record("compact", 73)), [{ type: "compaction_started" }]);
});

test("every compaction that starts ends, and a status frame alone says nothing", () => {
  for (const name of sdkRecordings()) {
    const all = events(name);
    assert.equal(of(all, "compaction_started").length, of(all, "compaction_ended").length, name);
  }
  assert.deepEqual(perRecord("clear")[1], []);
});

test("a permission mode the agent reports is a change of mode", () => {
  assert.deepEqual(perRecord("permission-mode-status"), [
    [{ type: "mode_changed", mode: "bypassPermissions" }],
    [{ type: "mode_changed", mode: "auto" }],
  ]);
});

test("a rate limit event says the cached limits are stale", () => {
  const made = perRecord("background");
  assert.equal(record("background", 4).type, "rate_limit_event");
  assert.deepEqual(made[4], [{ type: "limits_changed" }]);
});

// Prompts.

const REPLAYS: Readonly<Record<string, readonly string[]>> = {
  "prompt-replay-after-tools": [
    "dc59ce3f-b877-467b-9535-5a6fb31348b0",
    "4e799a6b-e0a9-4841-95b1-aa3b550b4c27",
    "b8405d56-8d14-41d9-a027-a09e1fdbd33b",
  ],
  "prompt-replay-at-init": [
    "33b2a126-789b-411f-93af-4cbacadf7cf6",
    "d54bbe7c-e036-4121-9cea-d9a3742f8c3c",
    "762ccb80-c5a4-429d-9976-d75f881ab266",
  ],
  "prompt-replay-before-notification": [
    "cc90a323-8661-45d6-ace7-cafed382fff6",
    "d0004825-a6e7-41ba-8580-04f39c6540c8",
    "b02397fb-75de-4211-a697-68e5cbf4e152",
  ],
  "prompt-replay-during-tool": [
    "4ef8c440-8292-4cbf-915c-1e94ca72c6c8",
    "eaea963d-d83d-4590-abfb-3a08240cbac8",
    "85f7619c-0926-4d3b-933f-34df46e93125",
  ],
  "prompt-replay-stop-queued": [
    "4a283d93-0990-4e11-99ed-815cb108f013",
    "42decd88-e1fb-4ad0-a509-8fe0832a29f2",
    "ff17e2b8-a01e-4227-83ba-92574bea3994",
  ],
};

for (const [name, sent] of Object.entries(REPLAYS)) {
  test(`a prompt the agent replays is taken, in the order it took them [${name}]`, () => {
    const all = events(name, sent);
    assert.deepEqual(
      of(all, "prompt_taken").map((event) => event.promptId),
      sent,
    );
  });
}

test("a prompt with a turn of its own is taken before that turn's first words", () => {
  for (const name of ["prompt-replay-after-tools", "prompt-replay-at-init"]) {
    const [first, report, second, third] = turns(events(name, REPLAYS[name]));
    for (const turn of [first, second, third]) {
      const kinds = types(turn ?? []).filter((type) => type !== "limits_changed");
      assert.deepEqual(
        kinds.slice(0, 3),
        ["session_started", "prompt_taken", "message_started"],
        name,
      );
    }
    assert.equal(of(report ?? [], "prompt_taken").length, 0, name);
    assert.equal(of(report ?? [], "turn_ended")[0]?.startedBy, "agent", name);
  }
});

test("a prompt taken into a running turn follows the result that took it in", () => {
  const name = "prompt-replay-during-tool";
  const [, report] = turns(events(name, REPLAYS[name]));
  const kinds = types(report ?? []);
  const taken = kinds.indexOf("prompt_taken");
  assert.equal(kinds[taken - 1], "call_ended");
  assert.equal(kinds.at(-1), "turn_ended");
  assert.equal(of(report ?? [], "turn_ended")[0]?.startedBy, "agent");
  assert.equal(of(report ?? [], "prompt_taken")[0]?.promptId, REPLAYS[name]?.[1]);
});

test("a prompt queued behind an interrupted turn is taken by the turn that follows", () => {
  const name = "prompt-replay-stop-queued";
  const [, report, next] = turns(events(name, REPLAYS[name]));
  assert.equal(of(report ?? [], "prompt_taken").length, 0);
  assert.equal(of(report ?? [], "turn_ended")[0]?.ending, "interrupted");
  assert.equal(of(next ?? [], "prompt_taken")[0]?.promptId, REPLAYS[name]?.[1]);
});

test("a replay of a prompt this session did not send gives no event", () => {
  for (const name of Object.keys(REPLAYS)) {
    assert.equal(of(events(name), "prompt_taken").length, 0, name);
  }
  // `/compact` comes back as its output, under an id the daemon never sent.
  const sent = [
    "12fadf24-cb50-46e5-961d-2f25cceb07fc",
    "eb24fc0a-65f1-41af-a20a-50594b50e1d6",
    "ead115be-94e7-4128-bee9-89438856d55e",
  ];
  const made = perRecord("compact", sent);
  assert.deepEqual(
    of(made.flat(), "prompt_taken").map((event) => event.promptId),
    sent,
  );
  assert.equal(record("compact", 78).isReplay, true);
  assert.deepEqual(made[78], []);
});

test("a taken prompt gives nothing else, whatever its record holds", () => {
  const translator = new Translator();
  const wire = record("tools", 24);
  translator.promptSent(wire.uuid as string);
  assert.deepEqual(translator.translate(wire), [
    { type: "prompt_taken", promptId: wire.uuid as string },
  ]);
});

test("what the agent tells itself between two messages gives no event", () => {
  // A hook's feedback, the summary after a compaction, the note of an interrupt, a subagent's
  // prompt: user records that are neither a prompt of the owner's nor a result.
  for (const [name, index] of [
    ["goal", 26],
    ["compact", 77],
    ["auto-compact", 6],
    ["interrupt", 33],
    ["skill-fork", 16],
  ] as const) {
    assert.equal(record(name, index).type, "user");
    assert.deepEqual(perRecord(name)[index], [], `${name} record ${index}`);
  }
});

// Errors.

test("a message about a failed login is an authentication error", () => {
  assert.deepEqual(perRecord("auth-failed")[2], [
    {
      type: "agent_error",
      kind: "authentication",
      category: "authentication_failed",
      text: "Not logged in · Please run /login",
      parentCallId: null,
    },
  ]);
});

const API_ERRORS: readonly (readonly [string, "authentication" | "other", string, string])[] = [
  ["api-error-400", "other", "unknown", "API Error: 400 Synthetic invalid request."],
  [
    "api-error-401",
    "authentication",
    "authentication_failed",
    "Failed to authenticate. API Error: 401 Synthetic authentication error.",
  ],
  ["api-error-402", "other", "unknown", "API Error: 402 Synthetic billing error."],
  [
    "api-error-403",
    "authentication",
    "authentication_failed",
    "Failed to authenticate. API Error: 403 Synthetic permission error.",
  ],
  ["api-error-404", "other", "model_not_found", "There's an issue with the selected model"],
  ["api-error-413", "other", "invalid_request", "Request too large (max 32MB)."],
  ["api-error-429", "other", "rate_limit", "API Error: Server is temporarily limiting requests"],
  ["api-error-500", "other", "server_error", "API Error: 500 Synthetic internal error."],
  ["api-error-504", "other", "server_error", "API Error: 504 Synthetic timeout."],
  ["server-error", "other", "server_error", "API Error: 529 Overloaded."],
];

for (const [name, kind, category, opening] of API_ERRORS) {
  test(`a failed request is an error under the agent's own category [${name}]`, () => {
    const all = events(name);
    const errors = of(all, "agent_error");
    assert.equal(errors.length, 1);
    assert.equal(errors[0]?.kind, kind);
    assert.equal(errors[0]?.category, category);
    assert.equal(errors[0]?.parentCallId, null);
    assert.ok(errors[0]?.text.startsWith(opening), errors[0]?.text);
    // The words are the error's alone: nothing writes them a second time before the result.
    assert.deepEqual(of(all, "text"), []);
    assert.deepEqual(of(all, "text_delta"), []);
    assert.equal(of(all, "turn_ended")[0]?.ending, "error");
    assert.equal(of(all, "turn_ended")[0]?.finalText, errors[0]?.text);
  });
}

test("a subagent's failed request is an error under the call that runs it", () => {
  const made = perRecord("subagent-api-error");
  assert.equal(made[21]?.length, 1);
  const [error] = of(made[21] ?? [], "agent_error");
  assert.equal(error?.kind, "other");
  assert.equal(error?.category, "server_error");
  assert.equal(error?.parentCallId, "toolu_synthetic161");
  assert.ok(error?.text.startsWith("API Error: 529 Overloaded."));
  // The turn that launched it had ended well, and the report turn does too.
  assert.deepEqual(
    of(made.flat(), "turn_ended").map((event) => event.ending),
    ["done", "done"],
  );
});

test("an error message gives no text and no call, whatever it holds", () => {
  const translator = new Translator();
  const wire = record("auth-failed", 2);
  const base = wire.message as JsonObject;
  const use = { type: "tool_use", id: "toolu_000", name: "Bash", input: {} };
  const content = [...(base.content as JsonObject[]), use];
  assert.deepEqual(types(translator.translate({ ...wire, message: { ...base, content } })), [
    "agent_error",
  ]);
});

test("an error message with no words is an error with none", () => {
  const translator = new Translator();
  const wire = record("auth-failed", 2);
  const message = { ...(wire.message as JsonObject), content: [] };
  const [error] = of(translator.translate({ ...wire, message }), "agent_error");
  assert.equal(error?.text, "");
  assert.equal(error?.kind, "authentication");
});

// Turns, over every recording.

test("every result gives exactly one end of turn", () => {
  for (const name of sdkRecordings()) {
    const wire = sdkRecords(name);
    perRecord(name).forEach((made, index) => {
      const ends = of(made, "turn_ended").length;
      assert.equal(ends, wire[index]?.type === "result" ? 1 : 0, `${name} record ${index}`);
      if (ends === 1) assert.equal(made.length, 1, `${name} record ${index}`);
    });
  }
});

test("the first record of a turn that Python's session starts it on gives an event", () => {
  // Python starts a turn on the first stream event, assistant, user or result record that no
  // prompt's replay accounts for and no call runs, or on a compaction when a prompt is waiting.
  // Here a turn starts on an event, so that record must give one, or the reply would open
  // later than it did.
  const starting = new Set(["stream_event", "assistant", "user", "result"]);
  for (const name of sdkRecordings()) {
    const wire = sdkRecords(name);
    const sent = wire
      .filter((entry) => entry.type === "user" && entry.isReplay === true)
      .map((entry) => entry.uuid as string);
    let open = false;
    perRecord(name, sent).forEach((made, index) => {
      const entry = wire[index];
      const taken = made.some((event) => event.type === "prompt_taken");
      const compaction = made.some(
        (event) => event.type === "compaction_started" || event.type === "compacted",
      );
      const frame = starting.has(entry?.type as string) && !entry?.parent_tool_use_id && !taken;
      if (frame && !open) {
        assert.ok(made.length > 0, `${name} record ${index} starts a turn and gives no event`);
      }
      if (frame || compaction) open = true;
      if (entry?.type === "result") open = false;
    });
  }
});

// A background subagent's hand-back (`subagent-handback`, recorded on 2026-10-10 with
// `@anthropic-ai/claude-agent-sdk` 0.3.296, Claude Code 2.1.296, the owner's setting sources
// loaded). The subagent's report reaches the main agent as a `user` record whose `origin` is a
// peer with the task's id, which starts a turn; the task's own notification then gets a turn
// that does nothing: an `init`, then a `result` with `num_turns` 0 and no text.

test("a subagent's hand-back names the task whose report the turn is", () => {
  const all = events("subagent-handback");
  const started = of(all, "task_started").find((event) => event.taskType === "local_agent");
  assert.ok(started);
  assert.deepEqual(of(all, "report_started"), [{ type: "report_started", taskId: started.taskId }]);
  // Before the first word of the turn it opens, and after the turn before it has ended.
  const at = all.findIndex((event) => event.type === "report_started");
  const ended = all.findIndex((event) => event.type === "turn_ended");
  const worded = all.findIndex((event, index) => index > at && event.type === "text_started");
  assert.ok(ended < at && at < worded);
});

test("a message from a peer that names no task of the session starts no report", () => {
  const peer = sdkRecords("subagent-handback").find(
    (found) => found.type === "user" && typeof found.origin === "object" && found.origin !== null,
  );
  assert.ok(peer);
  const origin = { ...(peer.origin as JsonObject) };
  delete origin.senderTaskId;
  assert.deepEqual(new Translator().translate({ ...peer, origin }), []);
  assert.deepEqual(new Translator().translate({ ...peer, origin: { kind: "channel" } }), []);
});

test("a turn's end says how many steps the agent took, none for a turn that did nothing", () => {
  const ends = of(events("subagent-handback"), "turn_ended");
  assert.deepEqual(
    ends.map((event) => [event.startedBy, event.steps, event.finalText === ""]),
    [
      ["owner", 3, false],
      ["agent", 1, false],
      ["agent", 0, true],
      ["agent", 1, false],
    ],
  );
});

test("a result that names no count of steps reads as one that took some", () => {
  const result = sdkRecords("tools").find((found) => found.type === "result");
  assert.ok(result);
  const { num_turns: _, ...bare } = result;
  const [ended] = of(new Translator().translate(bare), "turn_ended");
  assert.equal(ended?.steps, null);
});
