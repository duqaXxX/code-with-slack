/**
 * `ClaudeSession` on a fake `query()`, scripted from the recordings under `tests/fixtures/sdk/`.
 * Python had no test file for this wrapper: its behaviour was tested through `test_sessions.py`
 * on `FakeClaudeClient`, whose scripted batches, injected turns, permission calls, hook runs, end
 * of stream and failing stream `test/support/fake-query.ts` offers again.
 *
 * The options are those Python's `client_options` passed, and the SDK's option names are those
 * of the TypeScript SDK 0.3.296 (`sdk.d.ts`). The one difference: `allow-dangerously-skip-
 * permissions` is the SDK's own option `allowDangerouslySkipPermissions`, which writes the same
 * flag on the command line.
 */
import assert from "node:assert/strict";
import { type TestContext, test } from "node:test";
import {
  ClaudeSession,
  logger,
  SessionClosedError,
  type SessionConfig,
} from "../../../src/agent/claude/session.ts";
import { Translator } from "../../../src/agent/claude/translate.ts";
import type {
  PermissionAnswer,
  PermissionRequest,
  Prompt,
  QuestionAnswer,
  QuestionRequest,
  RequestHandler,
  SessionEvent,
} from "../../../src/agent/seam.ts";
import { ResumeRefused } from "../../../src/agent/seam.ts";
import {
  ask,
  type Batch,
  END,
  type FakeQuery,
  type FakeScript,
  FakeSdk,
  fail,
  hook,
} from "../../support/fake-query.ts";
import { type JsonObject, sdkJson, sdkRecords } from "../../support/fixtures.ts";

const LIMIT = { timeout: 10_000 };
const FOLDER = "/srv/alice/app";
const CONFIG: SessionConfig = {
  folder: FOLDER,
  resume: null,
  settingsSources: ["user", "project", "local"],
  model: null,
  effort: null,
  permissionMode: null,
  chrome: false,
};
const PROMPT: Prompt = { id: "00000000-0000-4000-8000-0000000000b1", content: "Say ok" };
const TOOLS = sdkRecords("tools");
const STOP = sdkJson("stop-hook") as JsonObject;
const POST_TOOL_USE = sdkJson("post-tool-use-hook") as JsonObject;
const ASK = sdkJson("ask-can-use-tool") as JsonObject;
const BASH = { command: "echo ok", description: "Print ok" };
const EXITED = "the Claude Code process exited";

// The record of the `tools` turn that holds its first tool call, and the one that answers it.
const FIRST_CALL = TOOLS.findIndex((record) => record.type === "assistant" && hasToolUse(record));

function hasToolUse(record: JsonObject): boolean {
  const message = record.message as JsonObject | undefined;
  const content = message?.content;
  return (
    Array.isArray(content) && content.some((block) => (block as JsonObject).type === "tool_use")
  );
}

/** A handler that records what it is asked and answers with `permission` and `question`. */
function handlerOf(
  permission: PermissionAnswer = { allow: true },
  question: QuestionAnswer = { answered: false, message: "skipped" },
): RequestHandler & { asked: (PermissionRequest | QuestionRequest)[] } {
  const asked: (PermissionRequest | QuestionRequest)[] = [];
  return {
    asked,
    permission: async (request) => {
      asked.push(request);
      return permission;
    },
    question: async (request) => {
      asked.push(request);
      return question;
    },
  };
}

interface Started {
  readonly session: ClaudeSession;
  readonly query: FakeQuery;
  readonly events: AsyncIterator<SessionEvent>;
}

/** A session on a fake query, ready, closed when the test ends. */
async function started(
  t: TestContext,
  script: FakeScript = {},
  config: Partial<SessionConfig> = {},
  requests: RequestHandler = handlerOf(),
): Promise<Started> {
  const sdk = new FakeSdk(script);
  const session = new ClaudeSession({ ...CONFIG, ...config }, requests, sdk.query);
  t.after(() => session.close());
  await session.ready();
  return { session, query: sdk.only, events: session.events[Symbol.asyncIterator]() };
}

/** The events up to and including the first of `type`, or all of them when the stream ends. */
async function until(
  events: AsyncIterator<SessionEvent>,
  type: SessionEvent["type"],
): Promise<SessionEvent[]> {
  const seen: SessionEvent[] = [];
  for (;;) {
    const next = await events.next();
    if (next.done) return seen;
    seen.push(next.value);
    if (next.value.type === type) return seen;
  }
}

async function drain(events: AsyncIterable<SessionEvent>): Promise<SessionEvent[]> {
  const seen: SessionEvent[] = [];
  for await (const event of events) seen.push(event);
  return seen;
}

function types(events: readonly SessionEvent[]): string[] {
  return events.map((event) => event.type);
}

// Options.

test(
  "a new session starts Claude Code in its folder with the owner's settings",
  LIMIT,
  async (t) => {
    const { query } = await started(t);
    const { options } = query;
    assert.equal(options.cwd, FOLDER);
    assert.deepEqual(options.settingSources, ["user", "project", "local"]);
    assert.equal(options.includePartialMessages, true);
    // Makes bypass possible, not active.
    assert.equal(options.allowDangerouslySkipPermissions, true);
    assert.deepEqual(options.extraArgs, { "replay-user-messages": null });
    assert.deepEqual(Object.keys(options).sort(), [
      "allowDangerouslySkipPermissions",
      "canUseTool",
      "cwd",
      "extraArgs",
      "hooks",
      "includePartialMessages",
      "settingSources",
      "stderr",
    ]);
    assert.deepEqual(Object.keys(options.hooks ?? {}).sort(), ["PostToolUse", "Stop"]);
  },
);

test("a resumed session passes the stored id, effort, model and mode", LIMIT, async (t) => {
  const { query } = await started(
    t,
    {},
    {
      resume: "68da9311-0000-4000-8000-0000000000aa",
      effort: "high",
      model: "haiku",
      permissionMode: "bypassPermissions",
    },
  );
  const { options } = query;
  assert.equal(options.resume, "68da9311-0000-4000-8000-0000000000aa");
  assert.equal(options.effort, "high");
  assert.equal(options.model, "haiku");
  assert.equal(options.permissionMode, "bypassPermissions");
});

test("the settings sources are those the seam gives", LIMIT, async (t) => {
  const { query } = await started(t, {}, { settingsSources: [] });
  assert.deepEqual(query.options.settingSources, []);
});

test("Chrome adds its flag to the extra arguments, and nothing else", LIMIT, async (t) => {
  const { query } = await started(t, {}, { chrome: true });
  assert.deepEqual(query.options.extraArgs, { "replay-user-messages": null, chrome: null });
});

test("an effort level Claude Code does not know is dropped with a warning", LIMIT, async (t) => {
  const warnings: string[] = [];
  t.mock.method(logger, "warning", (message: string) => warnings.push(message));
  const { query } = await started(t, {}, { effort: "turbo" });
  assert.equal("effort" in query.options, false);
  assert.deepEqual(warnings, ["dropped an unrecognized effort level"]);
});

test("the process's stderr is counted and never logged", LIMIT, async (t) => {
  const lines: string[] = [];
  t.mock.method(logger, "debug", (message: string) => lines.push(message));
  const { query } = await started(t);
  query.options.stderr?.("the conversation, quoted");
  assert.deepEqual(lines, ["claude stderr: 24 chars"]);
});

// Events.

test("a recorded turn gives the events its records give, in order", LIMIT, async (t) => {
  const { session, events } = await started(t, { turns: [TOOLS] });
  await session.send(PROMPT);
  const seen = await until(events, "turn_ended");
  const expected = new Translator();
  const records = TOOLS.flatMap((record) => expected.translate(record));
  // Claude Code's replay of the prompt comes with the turn and is the only addition.
  assert.deepEqual(
    seen.filter((event) => event.type !== "prompt_taken"),
    records,
  );
  assert.equal(seen[0]?.type, "session_started");
  assert.equal(seen.filter((event) => event.type === "turn_ended").length, 1);
  const calls = types(seen).filter((type) => type === "call_started" || type === "call_ended");
  assert.ok(calls.length >= 2);
  assert.equal(calls[0], "call_started");
});

test("a prompt that comes back is prompt_taken, before the words of its turn", LIMIT, async (t) => {
  const { session, query, events } = await started(t, { turns: [TOOLS] });
  await session.send(PROMPT);
  const seen = await until(events, "turn_ended");
  const taken = seen.filter((event) => event.type === "prompt_taken");
  assert.deepEqual(taken, [{ type: "prompt_taken", promptId: PROMPT.id }]);
  assert.ok(types(seen).indexOf("prompt_taken") < types(seen).indexOf("message_started"));
  // The SDK was given one user message, under the prompt's id.
  assert.equal(query.sent.length, 1);
  assert.equal(query.sent[0]?.uuid, PROMPT.id);
  assert.equal(query.sent[0]?.message.content, "Say ok");
  assert.equal(query.sent[0]?.parent_tool_use_id, null);
});

test("a batch nobody asked for gives its events at once", LIMIT, async (t) => {
  const { query, events } = await started(t);
  query.inject(TOOLS);
  const seen = await until(events, "turn_ended");
  assert.equal(seen.at(-1)?.type, "turn_ended");
  assert.equal(
    seen.some((event) => event.type === "prompt_taken"),
    false,
  );
});

test("two prompts get one batch each, in the order they were sent", LIMIT, async (t) => {
  const first = sdkRecords("usage");
  const { session, events } = await started(t, { turns: [first, TOOLS] });
  await session.send({ id: "00000000-0000-4000-8000-0000000000b2", content: "/usage" });
  const one = await until(events, "turn_ended");
  await session.send(PROMPT);
  const two = await until(events, "turn_ended");
  assert.equal(one.find((event) => event.type === "turn_ended")?.type, "turn_ended");
  assert.ok(two.some((event) => event.type === "call_started"));
  assert.ok(two.some((event) => event.type === "prompt_taken"));
});

// Requests.

test(
  "a permission request reaches the handler and an allow with a changed input is passed on",
  LIMIT,
  async (t) => {
    const handler = handlerOf({ allow: true, changedInput: { command: "echo changed" } });
    const batch: Batch = [
      ...TOOLS.slice(0, FIRST_CALL + 1),
      ask({
        toolName: "Bash",
        input: BASH,
        toolUseID: "toolu_000",
        requestId: "request-1",
        description: "Print ok",
      }),
      ...TOOLS.slice(FIRST_CALL + 1),
    ];
    const { session, query, events } = await started(t, { turns: [batch] }, {}, handler);
    await session.send(PROMPT);
    const seen = await until(events, "turn_ended");
    assert.deepEqual(handler.asked, [
      {
        type: "permission",
        requestId: "request-1",
        callId: "toolu_000",
        toolName: "Bash",
        input: BASH,
        title: null,
        description: "Print ok",
      },
    ]);
    // The SDK's result, field by field.
    assert.deepEqual(query.permissionResults, [
      { behavior: "allow", updatedInput: { command: "echo changed" } },
    ]);
    // The turn went on after the answer: the call and the end are both there.
    assert.ok(types(seen).includes("call_started"));
    assert.equal(seen.at(-1)?.type, "turn_ended");
  },
);

test("an allow with no changed input passes the call's own input back", LIMIT, async (t) => {
  const { session, query, events } = await started(t, {
    turns: [[...TOOLS.slice(0, 2), ask({ toolName: "Bash", input: BASH }), ...TOOLS.slice(-1)]],
  });
  await session.send(PROMPT);
  await until(events, "turn_ended");
  assert.deepEqual(query.permissionResults, [{ behavior: "allow", updatedInput: BASH }]);
});

test("a deny reaches the SDK with the owner's message", LIMIT, async (t) => {
  const handler = handlerOf({ allow: false, message: "No: the owner refused." });
  const { session, query, events } = await started(
    t,
    { turns: [[...TOOLS.slice(0, 2), ask({ toolName: "Bash", input: BASH }), ...TOOLS.slice(-1)]] },
    {},
    handler,
  );
  await session.send(PROMPT);
  await until(events, "turn_ended");
  assert.deepEqual(query.permissionResults, [
    { behavior: "deny", message: "No: the owner refused." },
  ]);
});

test("the stream waits for an answer the owner has not given", LIMIT, async (t) => {
  let answer: (value: PermissionAnswer) => void = () => undefined;
  const handler: RequestHandler = {
    permission: () => new Promise<PermissionAnswer>((resolve) => (answer = resolve)),
    question: async () => ({ answered: false, message: "" }),
  };
  const batch: Batch = [
    ...TOOLS.slice(0, FIRST_CALL + 1),
    ask({ toolName: "Bash", input: BASH }),
    TOOLS[78] as JsonObject,
  ];
  const { session, query, events } = await started(t, { turns: [batch] }, {}, handler);
  await session.send(PROMPT);
  const before = await until(events, "call_started");
  assert.equal(before.at(-1)?.type, "call_started");
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(query.permissionResults, []);
  answer({ allow: true });
  const after = await until(events, "turn_ended");
  assert.equal(after.at(-1)?.type, "turn_ended");
  assert.equal(query.permissionResults.length, 1);
});

test(
  "a question is answered through the updated input, a multi-select with a list",
  LIMIT,
  async (t) => {
    const input = ASK.input as Record<string, unknown>;
    const answers = {
      "Which colour do you prefer?": "Red",
      "Do you also like green?": ["Yes", "No"],
    };
    const handler = handlerOf({ allow: true }, { answered: true, answers });
    const { session, query, events } = await started(
      t,
      {
        turns: [
          [
            ...TOOLS.slice(0, 2),
            ask({ toolName: "AskUserQuestion", input, toolUseID: "toolu_001" }),
            ...TOOLS.slice(-1),
          ],
        ],
      },
      {},
      handler,
    );
    await session.send(PROMPT);
    await until(events, "turn_ended");
    const [request] = handler.asked;
    assert.equal(request?.type, "question");
    assert.equal(request?.callId, "toolu_001");
    assert.deepEqual(query.permissionResults, [
      { behavior: "allow", updatedInput: { questions: input.questions, answers } },
    ]);
  },
);

test("a question the owner skips is a deny with what the agent is told", LIMIT, async (t) => {
  const input = ASK.input as Record<string, unknown>;
  const handler = handlerOf({ allow: true }, { answered: false, message: "The owner skipped it." });
  const { session, query, events } = await started(
    t,
    {
      turns: [
        [...TOOLS.slice(0, 2), ask({ toolName: "AskUserQuestion", input }), ...TOOLS.slice(-1)],
      ],
    },
    {},
    handler,
  );
  await session.send(PROMPT);
  await until(events, "turn_ended");
  assert.deepEqual(query.permissionResults, [
    { behavior: "deny", message: "The owner skipped it." },
  ]);
});

test("a request with no request id of its own gets one", LIMIT, async (t) => {
  const handler = handlerOf();
  const call = ask({ toolName: "Bash", input: BASH });
  const bare = { ask: { ...call.ask, requestId: "" } };
  const { session, events } = await started(
    t,
    { turns: [[...TOOLS.slice(0, 2), bare, ...TOOLS.slice(-1)]] },
    {},
    handler,
  );
  await session.send(PROMPT);
  await until(events, "turn_ended");
  const [request] = handler.asked;
  assert.ok(request !== undefined && request.requestId !== "");
});

// The review round of 2026-10-10: a request that cannot be answered is a deny, never a rejection.
// SDK 0.3.296 answers a callback that rejects with a `control_response` of subtype `error`
// (`handleControlRequest`, sdk.mjs), which is not a deny.

const UNSHOWN = "awaydesk could not show this request in Slack, so nobody approved it.";
const SECRET = "a secret sentence from the tool's input";

/** One Bash call against `handler`, with the error log recorded; what the SDK was answered. */
async function askBash(t: TestContext, handler: RequestHandler, errors: string[]) {
  t.mock.method(logger, "error", (message: string) => errors.push(message));
  const { session, query, events } = await started(
    t,
    { turns: [[...TOOLS.slice(0, 2), ask({ toolName: "Bash", input: BASH }), ...TOOLS.slice(-1)]] },
    {},
    handler,
  );
  await session.send(PROMPT);
  await until(events, "turn_ended");
  return query.permissionResults;
}

test(
  "a handler that rejects is a deny, and the log has the error's name alone",
  LIMIT,
  async (t) => {
    const errors: string[] = [];
    const failure = Object.assign(new Error(SECRET), { name: "SlackDown" });
    const handler: RequestHandler = {
      permission: async () => {
        throw failure;
      },
      question: async () => ({ answered: false, message: "" }),
    };
    assert.deepEqual(await askBash(t, handler, errors), [{ behavior: "deny", message: UNSHOWN }]);
    assert.equal(errors.length, 1);
    assert.ok(errors[0]?.includes("SlackDown"));
    assert.ok(!errors[0]?.includes(SECRET));
  },
);

test("a handler that throws at once is a deny", LIMIT, async (t) => {
  const errors: string[] = [];
  const handler: RequestHandler = {
    permission: () => {
      throw new TypeError(SECRET);
    },
    question: async () => ({ answered: false, message: "" }),
  };
  assert.deepEqual(await askBash(t, handler, errors), [{ behavior: "deny", message: UNSHOWN }]);
  assert.equal(errors.length, 1);
  assert.ok(errors[0]?.includes("TypeError"));
  assert.ok(!errors[0]?.includes(SECRET));
});

test("a question whose input cannot be read is a deny", LIMIT, async (t) => {
  const errors: string[] = [];
  t.mock.method(logger, "error", (message: string) => errors.push(message));
  const handler = handlerOf();
  const { session, query, events } = await started(
    t,
    {
      turns: [
        [
          ...TOOLS.slice(0, 2),
          ask({ toolName: "AskUserQuestion", input: null as never }),
          ...TOOLS.slice(-1),
        ],
      ],
    },
    {},
    handler,
  );
  await session.send(PROMPT);
  await until(events, "turn_ended");
  assert.deepEqual(query.permissionResults, [{ behavior: "deny", message: UNSHOWN }]);
  assert.deepEqual(handler.asked, []);
  assert.deepEqual(errors, ["could not ask the owner about a call: TypeError"]);
});

test(
  "the deny message of a request that could not be shown is the session's option",
  LIMIT,
  async (t) => {
    t.mock.method(logger, "error", () => undefined);
    const sdk = new FakeSdk({
      turns: [[...TOOLS.slice(0, 2), ask({ toolName: "Bash", input: BASH }), ...TOOLS.slice(-1)]],
    });
    const handler: RequestHandler = {
      permission: async () => {
        throw new Error("down");
      },
      question: async () => ({ answered: false, message: "" }),
    };
    const session = new ClaudeSession(CONFIG, handler, sdk.query, "Nobody saw it.");
    t.after(() => session.close());
    await session.ready();
    await session.send(PROMPT);
    await until(session.events[Symbol.asyncIterator](), "turn_ended");
    assert.deepEqual(sdk.only.permissionResults, [{ behavior: "deny", message: "Nobody saw it." }]);
  },
);

// Hooks.

test(
  "the Stop hook gives the effort and the folder, where it ran in the stream",
  LIMIT,
  async (t) => {
    const batch: Batch = [...TOOLS.slice(0, -1), hook("Stop", STOP), TOOLS.at(-1) as JsonObject];
    const { session, events } = await started(t, { turns: [batch] });
    await session.send(PROMPT);
    const seen = await until(events, "turn_ended");
    const tail = seen.slice(-3);
    assert.deepEqual(tail, [
      { type: "effort_observed", level: "medium" },
      { type: "folder_changed", folder: "/home/dev/project" },
      seen.at(-1),
    ]);
    assert.equal(seen.at(-1)?.type, "turn_ended");
  },
);

test("the PostToolUse hook gives the folder alone", LIMIT, async (t) => {
  const batch: Batch = [...TOOLS.slice(0, 3), hook("PostToolUse", POST_TOOL_USE)];
  const { session, events } = await started(t, { turns: [batch] });
  await session.send(PROMPT);
  const seen = await until(events, "folder_changed");
  assert.deepEqual(seen.at(-1), { type: "folder_changed", folder: "/home/dev/project" });
  assert.equal(
    seen.some((event) => event.type === "effort_observed"),
    false,
  );
});

// The end of the process.

test("a stream that ends gives process_lost, then ends", LIMIT, async (t) => {
  const errors: string[] = [];
  t.mock.method(logger, "error", (message: string) => errors.push(message));
  const { session, events } = await started(t, { turns: [[...TOOLS.slice(0, 3), END]] });
  await session.send(PROMPT);
  const seen = await until(events, "process_lost");
  assert.deepEqual(seen.at(-1), { type: "process_lost", reason: EXITED });
  assert.deepEqual(await events.next(), { done: true, value: undefined });
  assert.deepEqual(errors, [`session stopped: ${EXITED}`]);
});

test("a stream that throws gives process_lost with the error's name", LIMIT, async (t) => {
  const errors: string[] = [];
  t.mock.method(logger, "error", (message: string) => errors.push(message));
  const failure = Object.assign(new Error("exit 1: a secret sentence"), {
    name: "ProcessExitError",
  });
  const { session, events } = await started(t, { turns: [[...TOOLS.slice(0, 3), fail(failure)]] });
  await session.send(PROMPT);
  const seen = await until(events, "process_lost");
  assert.deepEqual(seen.at(-1), { type: "process_lost", reason: "ProcessExitError" });
  // The name, never the message.
  assert.deepEqual(errors, ["session stopped: ProcessExitError"]);
});

test("a session that was closed gives no process_lost", LIMIT, async (t) => {
  const { session, events, query } = await started(t, { turns: [TOOLS.slice(0, 3)] });
  await session.send(PROMPT);
  await until(events, "prompt_taken");
  await session.close();
  assert.equal(query.closed, true);
  const rest = await drain(session.events);
  assert.equal(
    rest.some((event) => event.type === "process_lost"),
    false,
  );
});

test("a prompt sent after the process was lost, or after close, is refused", LIMIT, async (t) => {
  t.mock.method(logger, "error", () => undefined);
  const lost = await started(t, { start: [END] });
  await until(lost.events, "process_lost");
  await assert.rejects(lost.session.send(PROMPT), SessionClosedError);
  const closed = await started(t);
  await closed.session.close();
  await assert.rejects(closed.session.send(PROMPT), SessionClosedError);
});

test("close ends the input and resolves only once the stream has ended", LIMIT, async (t) => {
  const { session, query } = await started(t, { holdClose: true });
  let resolved = false;
  const closing = session.close().then(() => {
    resolved = true;
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(query.inputEnded, true);
  assert.equal(query.closed, true);
  assert.equal(resolved, false);
  query.finishClose();
  await closing;
  assert.equal(resolved, true);
});

test("close is idempotent: the query is closed once", LIMIT, async (t) => {
  const { session, query } = await started(t);
  await session.close();
  await session.close();
  assert.equal(query.calls.filter((call) => call.method === "close").length, 1);
});

// Commands.

test("interrupt and stop_task reach Claude Code", LIMIT, async (t) => {
  const { session, query } = await started(t);
  await session.interrupt();
  await session.stopTask("task-1");
  assert.deepEqual(query.calls, [
    { method: "interrupt" },
    { method: "stopTask", taskId: "task-1" },
  ]);
});

test(
  "a permission mode reaches Claude Code, and one it does not know is refused",
  LIMIT,
  async (t) => {
    const { session, query } = await started(t);
    await session.setPermissionMode("bypassPermissions");
    await session.setPermissionMode("auto");
    await assert.rejects(session.setPermissionMode("everything"), RangeError);
    assert.deepEqual(query.calls, [
      { method: "setPermissionMode", mode: "bypassPermissions" },
      { method: "setPermissionMode", mode: "auto" },
    ]);
  },
);

test("a model reaches Claude Code, none meaning its default", LIMIT, async (t) => {
  const { session, query } = await started(t);
  await session.setModel("haiku");
  await session.setModel(null);
  assert.deepEqual(query.calls, [
    { method: "setModel", model: "haiku" },
    { method: "setModel", model: undefined },
  ]);
});

test("an effort level is applied to the live session, none clearing it", LIMIT, async (t) => {
  const { session, query } = await started(t);
  await session.setEffort("high");
  await session.setEffort(null);
  await assert.rejects(session.setEffort("turbo"), RangeError);
  assert.deepEqual(query.calls, [
    { method: "applyFlagSettings", effortLevel: "high" },
    { method: "applyFlagSettings", effortLevel: null },
  ]);
});

test("a command Claude Code refuses rejects with its error", LIMIT, async (t) => {
  const refusal = new Error("auto mode is not available on this model");
  const { session } = await started(t, { rejects: { setPermissionMode: refusal } });
  await assert.rejects(session.setPermissionMode("auto"), (error) => error === refusal);
});

test("the context usage is the model and the share of its window in use", LIMIT, async (t) => {
  const { session, query } = await started(t);
  assert.deepEqual(await session.contextUsage(), {
    model: "claude-haiku-4-5-20251001",
    percentage: 7,
  });
  assert.deepEqual(query.calls, [{ method: "getContextUsage" }]);
});

test(
  "the info holds the models, the commands with their aliases and the mode",
  LIMIT,
  async (t) => {
    const { session } = await started(t);
    const info = await session.info();
    assert.equal(info.permissionMode, "bypassPermissions");
    assert.ok(info.models.length > 0);
    assert.ok(info.models.every((model) => model.value !== ""));
    assert.ok(info.commands.length > 0);
    assert.ok(info.commands.some((command) => command.aliases.length > 0));
  },
);

// Start.

test(
  "a resume Claude Code refuses is ResumeRefused, and the process is closed",
  LIMIT,
  async () => {
    const failure = new Error("Claude Code returned an error result: No conversation found");
    const refusal = sdkRecords("interrupt").at(-1) as JsonObject;
    assert.equal(refusal.type, "result");
    const sdk = new FakeSdk({
      initError: failure,
      start: [{ ...refusal, is_error: true }, fail(failure)],
    });
    const session = new ClaudeSession(
      { ...CONFIG, resume: "68da9311-0000-4000-8000-0000000000aa" },
      handlerOf(),
      sdk.query,
    );
    await assert.rejects(session.ready(), (error) => {
      assert.ok(error instanceof ResumeRefused);
      assert.equal(error.cause, failure);
      return true;
    });
    assert.equal(sdk.only.closed, true);
  },
);

test("a start that fails with no session to resume rethrows the failure", LIMIT, async () => {
  const failure = new Error("Claude Code returned an error result: something");
  const refusal = sdkRecords("interrupt").at(-1) as JsonObject;
  const sdk = new FakeSdk({
    initError: failure,
    start: [{ ...refusal, is_error: true }, fail(failure)],
  });
  const session = new ClaudeSession(CONFIG, handlerOf(), sdk.query);
  await assert.rejects(session.ready(), (error) => error === failure);
  assert.equal(sdk.only.closed, true);
});

test(
  "a resume that fails to start with no error result is not a refused resume",
  LIMIT,
  async () => {
    const failure = new Error("spawn claude ENOENT");
    const sdk = new FakeSdk({ initError: failure, start: [fail(failure)] });
    const session = new ClaudeSession(
      { ...CONFIG, resume: "68da9311-0000-4000-8000-0000000000aa" },
      handlerOf(),
      sdk.query,
    );
    await assert.rejects(session.ready(), (error) => error === failure);
  },
);

test("a query whose close throws does not make the pump reject", LIMIT, async (t) => {
  const errors: string[] = [];
  t.mock.method(logger, "error", (message: string) => errors.push(message));
  const failure = Object.assign(new Error(SECRET), { name: "KillFailed" });
  const { session, events } = await started(t, {
    turns: [[...TOOLS.slice(0, 3), END]],
    closeThrows: failure,
  });
  await session.send(PROMPT);
  await until(events, "process_lost");
  assert.deepEqual(await events.next(), { done: true, value: undefined });
  await assert.doesNotReject(session.close());
  assert.ok(errors.some((line) => line.includes("KillFailed")));
  assert.ok(!errors.some((line) => line.includes(SECRET)));
});

test("a query whose close throws does not make a close of the session reject", LIMIT, async (t) => {
  const errors: string[] = [];
  t.mock.method(logger, "error", (message: string) => errors.push(message));
  const failure = Object.assign(new Error(SECRET), { name: "KillFailed" });
  const { session } = await started(t, { turns: [], closeThrows: failure });
  await assert.doesNotReject(session.close());
  assert.ok(errors.some((line) => line.includes("KillFailed")));
  assert.ok(!errors.some((line) => line.includes(SECRET)));
});
