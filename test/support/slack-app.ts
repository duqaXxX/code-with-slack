/**
 * The harness of the handlers' tests: the real app `buildApp` makes, over `FakeSlack`, with a real
 * `SessionManager` on the scripted agent back end of the sessions harness (`sessions.ts`). Port of
 * `World`, its fixtures and the payload builders of `tests/test_slack_app.py`.
 *
 * A payload goes in through Bolt's own entry point, `App.processEvent({ body, ack })`, the call
 * `SocketModeReceiver` makes for each `slack_event` (`@slack/bolt` 5.1.0,
 * `dist/receivers/SocketModeReceiver.js`): which listener takes which `action_id`,
 * `callback_id` or event type is under test with the listener itself. The receiver the app is
 * built with opens no socket. What was passed to `ack` is what `dispatch` answers.
 *
 * Time. Python's `dispatch` slept 50 ms for the listener's task to run. Here `dispatch` waits
 * for the daemon to come to rest (`idle`): every promise callback and every task that is ready
 * has run, and no file or git call is in flight (`process.getActiveResourcesInfo`, read against
 * what the test runner itself keeps open). Nothing
 * waits on the wall clock. Four fake clocks stand for it: `clock` is the sessions' (the idle
 * close, a stop's wait for the reply's end, the drain's poll), `slackClock` the provider's (a
 * reply's debounce), `appClock` the handlers' (a clip's wait, the rows of an opening modal, a
 * slow download), `homeClock` the session index's.
 *
 * What it takes from the sessions harness: `FakeAgentBackend` and `FakeAgentSession`, wrapped
 * (`WorldBackend`) so that each client is scripted when it is made, from the world's
 * `connectGate`, `connectError` and `permissionMode`, as Python's client factory read them.
 */
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import type { TestContext } from "node:test";
import type { App, Receiver } from "@slack/bolt";
import { agentInfo } from "../../src/agent/claude/info.ts";
import type {
  AgentSession,
  ListedSession,
  PromptContent,
  Question,
  RequestHandler,
  StartOptions,
} from "../../src/agent/seam.ts";
import { type BuiltApp, buildApp } from "../../src/chat/slack/app/app.ts";
import { ChannelGuard, type Identity } from "../../src/chat/slack/app/guards.ts";
import { HOLD_CONTINUE } from "../../src/chat/slack/hold.ts";
import { Home } from "../../src/chat/slack/home.ts";
import type { Listings } from "../../src/chat/slack/openfile/listing.ts";
import {
  CHOICE_ACTION,
  CHOICE_BLOCK,
  modalView,
  OPEN_BUTTON_ACTION,
  OPEN_FORM,
  QUERY_ACTION,
  QUERY_BLOCK,
  Target,
} from "../../src/chat/slack/openfile/modal.ts";
import { type Limiter, UpdateLimiter } from "../../src/chat/slack/reply/limiter.ts";
import { resetStatusFlags } from "../../src/chat/slack/reply/status.ts";
import { type Draft, dumpDraft } from "../../src/chat/slack/requests.ts";
import {
  SETUP_BLOCK,
  SETUP_BYPASS,
  SETUP_EFFORT,
  SETUP_MODEL,
  SETUP_START,
} from "../../src/chat/slack/setup.ts";
import { SlackChat } from "../../src/chat/slack/thread.ts";
import type { Config } from "../../src/core/config.ts";
import { UsageCache } from "../../src/core/footer.ts";
import { Holds } from "../../src/core/hold.ts";
import { Approvals } from "../../src/core/requests.ts";
import { SessionManager } from "../../src/core/sessions/manager.ts";
import { StateStore } from "../../src/core/state.ts";
import * as texts from "../../src/core/texts.ts";
import { setWriter } from "../../src/log.ts";
import {
  type AsyncEvent,
  BOT,
  CHANNEL,
  FakeSlack,
  OTHER_THREAD,
  OWNER,
  rejected,
  TEAM,
  THREAD,
} from "./fake-slack.ts";
import { type JsonObject, sdkJson, slackPayload, slackPayloads } from "./fixtures.ts";
import { commitAt, gitInit } from "./git-layouts.ts";
import {
  FakeAgentBackend,
  FakeAgentSession,
  type Script,
  SettledClock,
  sdkMessages,
} from "./sessions.ts";

// biome-ignore lint/suspicious/noExplicitAny: a Slack payload built or read in a test
export type Body = Record<string, any>;

// The click fixtures (000-005-block_actions.json) sit at their message's own ts, no thread_ts:
// `clickThread` reads it from `container.thread_ts`.
export const CLICK_THREAD = "1790192011.564799";
// form-open-click.json carries no thread_ts at all: `clickThread` falls back to `message.ts`.
export const FORM_THREAD = "1790285951.735939";
export const HOME_THREAD = "1790000000.000001";
export const SESSION_A = "68da9311-0000-4000-8000-00000000000a";
export const SESSION_B = "68da9311-0000-4000-8000-00000000000b";
// The permalink FakeSlack answers with by default (tests/fixtures/slack/api-chat-getPermalink.json).
export const PERMALINK = "https://example.slack.com/archives/C000CHAN/p1780000000000001";

/** The first recording of a kind, as a copy a test may change. */
export function recorded(kind: string): Body {
  const name = slackPayloads().find((candidate) => candidate.endsWith(`-${kind}`));
  if (name === undefined) throw new Error(`no recording of ${kind}`);
  return structuredClone(slackPayload(name));
}

/** A session as the agent lists it: Python's `SDKSessionInfo(session_id, summary, last_modified, file_size, custom_title)`. */
export function listed(
  id: string,
  title: string,
  lastModified: number,
  size: number,
  fields: Partial<Pick<ListedSession, "customTitle" | "branch">> = {},
): ListedSession {
  return { id, title, lastModified, size, customTitle: null, branch: null, ...fields };
}

/** What a dispatch was answered with: everything the listener passed to `ack`, in order. */
export class Dispatched {
  readonly acks: unknown[] = [];
  /** What Bolt's own processing rejected with, when it did. */
  error: unknown = null;

  /** Whether a listener (or Bolt, for an event) acknowledged the payload. */
  get acknowledged(): boolean {
    return this.acks.length > 0;
  }

  /** Python's `response.status`: 200 once acknowledged, 404 when no listener took the payload. */
  get status(): number {
    return this.acknowledged ? 200 : 404;
  }

  /** Python's `json.loads(response.body)`: what the acknowledgement carried, null when nothing. */
  get body(): Body | null {
    const last = this.acks.at(-1);
    return last === undefined || last === null ? null : (last as Body);
  }
}

/** The back end of a world: each client is scripted when it is made, as Python's factory did. */
class WorldBackend extends FakeAgentBackend {
  script: () => Script = () => ({});

  constructor() {
    super([]);
  }

  override async start(options: StartOptions, requests: RequestHandler): Promise<AgentSession> {
    const script = this.script();
    const session = new FakeAgentSession(options, requests, script);
    this.sessions.push(session);
    if (script.startGate !== undefined) await script.startGate.wait();
    if (script.startError !== undefined) throw script.startError;
    session.connected = true;
    return session;
  }
}

// The work of the daemon that a turn of the event loop does not finish: a file call and the
// close of its handle, a git process and its pipes. The test runner's own pipes are there from
// the start, so what counts is what is in flight beyond them.
const WAITED_FOR = new Set([
  "FSReqPromise",
  "FSReqCallback",
  "CloseReq",
  "ProcessWrap",
  "PipeWrap",
  "WriteWrap",
  "ShutdownWrap",
]);

function inFlight(): number {
  return process.getActiveResourcesInfo().filter((name) => WAITED_FOR.has(name)).length;
}

const AT_REST = inFlight();
// Turns of the event loop with nothing in flight before the daemon counts as at rest.
const CALM_TURNS = 40;

function turn(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

export interface WorldOptions {
  /** Whether `CHANNEL` is bound to `root/app`. */
  readonly bound?: boolean;
  /** The `chat.update` budget, when a test paces it. */
  readonly limiter?: Limiter;
  /** `!open`'s listings, when a test stands in for them. */
  readonly listings?: Listings;
}

export class World {
  readonly slack: FakeSlack;
  readonly tmpPath: string;
  /** The allowed root; `root/app` is the channel's folder. */
  readonly root: string;
  readonly state: StateStore;
  readonly approvals = new Approvals();
  readonly holds = new Holds();
  readonly backend = new WorldBackend();
  /** What each file URL downloads to: bytes, or the failure the download rejects with. */
  readonly downloads = new Map<string, Uint8Array | Error>();
  readonly uploads: string;
  readonly fetched: string[] = [];
  /** Seconds of `appClock` every download takes. */
  slowDownloads = 0;
  /** Holds every client's start until set, as a CLI that is still starting. */
  connectGate: AsyncEvent | null = null;
  /** Every client made from here on fails to start with this, as a session whose stored id no longer resumes. */
  connectError: Error | null = null;
  /** The mode every client made from here on says it started in. */
  permissionMode = "default";
  /**
   * Presses Start on every session setup that a dispatch leaves waiting, so the tests of
   * everything after the setup keep reading as they did; the setup's own tests turn it off.
   */
  autoStart = true;
  /** The sessions' clock. */
  readonly clock = new SettledClock();
  /** The Slack provider's clock. */
  readonly slackClock = new SettledClock();
  /** The handlers' clock. */
  readonly appClock = new SettledClock();
  readonly homeClock = new SettledClock();
  /** Wall-clock seconds as the handlers read them. */
  now: () => number = () => Date.now() / 1000;
  readonly limiter: Limiter;
  readonly identity: Identity = { ownerUserId: OWNER, teamId: TEAM, botUserId: BOT };
  readonly config: Config;
  readonly sessions: SessionManager;
  readonly home: Home;
  readonly deleted: Array<[string, string]> = [];
  readonly cleaned: string[] = [];
  readonly built: BuiltApp;
  readonly app: App;
  /** Whether the app's receiver was started, and stopped. */
  readonly receiver = { started: 0, stopped: 0 };
  readonly #started = new Set<string>();

  constructor(slack: FakeSlack, tmpPath: string, options: WorldOptions = {}) {
    this.slack = slack;
    this.tmpPath = tmpPath;
    this.root = join(tmpPath, "root");
    mkdirSync(join(this.root, "app"), { recursive: true });
    this.state = new StateStore(join(tmpPath, "state.json"));
    if (options.bound ?? true) this.state.bind(CHANNEL, join(this.root, "app"));
    this.uploads = join(tmpPath, "uploads");
    this.backend.script = () => {
      const recording = sdkJson("server-info") as JsonObject;
      return {
        info: agentInfo({
          commands: recording.commands,
          models: recording.models,
          current_permission_mode: this.permissionMode,
        }),
        ...(this.connectGate !== null && { startGate: this.connectGate }),
        ...(this.connectError !== null && { startError: this.connectError }),
      };
    };
    // A generous burst: these tests are about the handlers, not the shared limiter's pacing.
    this.limiter = options.limiter ?? new UpdateLimiter({ burst: 1_000, clock: this.slackClock });
    const chat = new SlackChat({
      slack,
      replies: slack,
      identity: this.identity,
      limiter: this.limiter,
      clock: this.slackClock,
    });
    this.sessions = new SessionManager({
      chat,
      agent: this.backend,
      state: this.state,
      approvals: this.approvals,
      holds: this.holds,
      usage: new UsageCache(async () => ({ session: null, week: null })),
      clock: this.clock,
    });
    this.config = {
      botToken: `${"xox"}b-fake`,
      appToken: `${"xap"}p-fake`,
      ownerUserId: OWNER,
      allowedRoot: this.root,
      configDir: tmpPath,
      userToken: null,
    };
    this.home = new Home(slack, {
      ownerUserId: OWNER,
      teamId: TEAM,
      state: this.state,
      sessionsOf: () => this.storedSessions,
      delete: async (channelId, threadTs) => {
        this.deleted.push([channelId, threadTs]);
        return null;
      },
      clean: async (channelId) => {
        this.cleaned.push(channelId);
        return null;
      },
      clock: this.homeClock,
    });
    const receiver: Receiver = {
      init: () => {},
      start: async () => {
        this.receiver.started += 1;
      },
      stop: async () => {
        this.receiver.stopped += 1;
      },
    };
    this.built = buildApp({
      slack,
      config: this.config,
      identity: this.identity,
      sessions: this.sessions,
      approvals: this.approvals,
      holds: this.holds,
      guard: new ChannelGuard(slack, this.identity),
      state: this.state,
      uploads: this.uploads,
      home: this.home,
      limiter: this.limiter,
      receiver,
      fetch: (request) => this.fetch(request),
      clock: this.appClock,
      now: () => this.now(),
      ...(options.listings !== undefined && { listings: options.listings }),
    });
    this.app = this.built.app;
  }

  /** Every agent session started, in order: Python's `world.clients`. */
  get clients(): FakeAgentSession[] {
    return this.backend.sessions;
  }

  /** What the agent lists for the channel's directory, newest first. */
  get storedSessions(): ListedSession[] {
    return this.backend.listed;
  }

  set storedSessions(sessions: ListedSession[]) {
    this.backend.listed = sessions;
  }

  async fetch(request: { url: string; mimetype: string; limit: number }): Promise<Uint8Array> {
    this.fetched.push(request.url);
    if (this.slowDownloads > 0) await this.appClock.sleep(this.slowDownloads);
    const found = this.downloads.get(request.url);
    if (found === undefined) throw new Error(`no download scripted for ${request.url}`);
    if (found instanceof Error) throw found;
    return found;
  }

  /**
   * Let the daemon come to rest: what is ready runs, every task that starts on a later turn of
   * the event loop too, and every file or git call in flight comes back. Where Python slept a
   * moment for the daemon to act.
   */
  async idle(): Promise<void> {
    let calm = 0;
    for (let turns = 0; calm < CALM_TURNS; turns += 1) {
      if (turns > 2_000_000) throw new Error("the daemon never came to rest");
      await turn();
      calm = inFlight() > AT_REST ? 0 : calm + 1;
    }
  }

  /**
   * Feed one payload to the app as the socket would, let the daemon come to rest, and answer
   * what the payload was acknowledged with. A listener that waits for the owner (a setup with
   * `autoStart` off, a hold) is still running when this returns, as it was in Python.
   */
  async dispatch(body: Body): Promise<Dispatched> {
    const response = new Dispatched();
    this.app
      .processEvent({
        body,
        ack: async (answer?: unknown) => {
          response.acks.push(answer ?? null);
        },
      })
      .catch((error: unknown) => {
        response.error = error;
      });
    await this.idle();
    if (this.autoStart) await this.startWaitingSetups();
    return response;
  }

  /** `[setup id, channel, thread, message ts]` of every setup message that was posted and stands. */
  waitingSetups(): Array<[string, string, string, string]> {
    const shown = new Map<string, string>();
    for (const [ts, message] of this.slack.messages) {
      if (message.deleted) continue;
      for (const block of message.blocks as Body[]) {
        for (const element of (block.elements ?? []) as Body[]) {
          if (element.action_id === SETUP_START) shown.set(element.value, ts);
        }
      }
    }
    const found: Array<[string, string, string, string]> = [];
    for (const post of this.slack.callsTo("chat.postMessage") as Body[]) {
      for (const block of (post.blocks ?? []) as Body[]) {
        for (const element of (block.elements ?? []) as Body[]) {
          const ts = shown.get(element.value);
          if (element.action_id === SETUP_START && ts !== undefined) {
            found.push([element.value, post.channel, post.thread_ts, ts]);
          }
        }
      }
    }
    return found;
  }

  /**
   * What Python's `settle(seconds)` waited through: the daemon comes to rest, Start is pressed on
   * every setup that showed up meanwhile, and again until none is left to press.
   */
  async settle(): Promise<void> {
    for (let round = 0; round < 5; round += 1) {
      await this.idle();
      await this.startWaitingSetups();
    }
  }

  async startWaitingSetups(): Promise<void> {
    for (const [setupId, channel, threadTs, ts] of this.waitingSetups()) {
      if (this.#started.has(setupId)) continue;
      this.#started.add(setupId);
      await this.dispatch(clickIn(SETUP_START, setupId, channel, threadTs, { messageTs: ts }));
    }
  }

  /**
   * Wait for a condition on the fakes, as Python's `until` polled it: the daemon comes to rest,
   * and Slack's clock moves on a hundredth of a second at a time, for two seconds of it at most,
   * so a reply's debounce passes as it did while Python polled.
   */
  async until(condition: () => boolean, limit = 2.0): Promise<void> {
    await this.idle();
    for (let waited = 0; !condition(); waited += 0.01) {
      if (waited >= limit) throw new Error("the condition never held");
      await this.slackClock.advance(0.01);
      await this.idle();
    }
  }

  /** Each prompt every client was sent, in the order the clients were made. */
  queries(): PromptContent[] {
    return this.clients.flatMap((client) => client.queries);
  }

  ephemerals(): string[] {
    return this.slack.callsTo("chat.postEphemeral").map((args) => String(args.text));
  }

  postedAnything(): boolean {
    return this.slack.apiCalls.some((call) => call.method.startsWith("chat."));
  }

  async close(): Promise<void> {
    this.connectGate?.set(); // a failed test must not leave the teardown waiting on it
    this.built.close();
    await this.sessions.closeAll();
    await this.home.close();
  }
}

/**
 * A world maker for one test, as the `world` fixture and `World(slack, tmp_path, ...)` were:
 * each world it made is closed and its folder removed when the test ends.
 */
export function worldFor(t: TestContext): ((options?: WorldOptions) => World) & {
  readonly slack: FakeSlack;
  readonly tmpPath: string;
} {
  const made: World[] = [];
  const slack = new FakeSlack();
  const tmpPath = realpathSync(mkdtempSync(join(tmpdir(), "awd-app-")));
  // The two "stop for the rest of the run" flags are the process's: each test starts clear.
  resetStatusFlags();
  // The daemon's log stays out of the test report, as pytest kept it: a test that reads a line
  // replaces the logger's method (`mock.method`), which this does not touch.
  const writer = setWriter(() => {});
  // A timer that keeps the loop alive for the test's duration: on Node 22 a test that awaits
  // something which never comes, with only unref'd timers pending, lets the process finish and
  // cancels the rest of the file. With this it waits for the runner's own timeout instead.
  const guard = setInterval(() => {}, 1_000);
  t.after(async () => {
    clearInterval(guard);
    for (const world of made) await world.close();
    rmSync(tmpPath, { recursive: true, force: true });
    setWriter(writer);
  });
  return Object.assign(
    (options: WorldOptions = {}) => {
      const world = new World(slack, tmpPath, options);
      made.push(world);
      return world;
    },
    { slack, tmpPath },
  );
}

/** The `world` fixture: one bound world for the test. */
export function worldOf(t: TestContext): World {
  return worldFor(t)();
}

/** The `manual` fixture: a world whose setups wait for the test to press Start. */
export function manualWorld(t: TestContext): World {
  const world = worldOf(t);
  world.autoStart = false;
  return world;
}

/**
 * The blocks Slack's composer sends with a message of one run of text, as the recorded events
 * hold them; a style as read back from Slack on 2026-10-07 (`code` for inline code).
 */
export function composed(text: string, style: Record<string, boolean> = {}): Body[] {
  const leaf: Body = { type: "text", text, ...(Object.keys(style).length > 0 ? { style } : {}) };
  const section = { type: "rich_text_section", elements: [leaf] };
  return [{ type: "rich_text", block_id: "pHDTI", elements: [section] }];
}

/**
 * A top-level message: its `thread_ts` defaults to its own `ts` (the fixture's ts unless `ts` is
 * given), so each call with a distinct `ts` opens an independent thread.
 */
export function message(text = "hello", event: Body = {}): Body {
  const body = recorded("event_callback-message");
  Object.assign(body.event, { text, blocks: composed(text), ...event });
  return body;
}

let replySeq = 0;

/** A reply inside `threadTs`'s thread: its own `ts` always differs from it. */
export function reply(text: string, threadTs: string, event: Body = {}): Body {
  const body = recorded("event_callback-message");
  replySeq += 1;
  const ownTs = `179019${String(replySeq).padStart(4, "0")}.900000`;
  Object.assign(body.event, {
    text,
    blocks: composed(text),
    thread_ts: threadTs,
    ts: ownTs,
    ...event,
  });
  return body;
}

/** A recorded reply inside a thread (its own shape, faithfully reused): 010 or 012. */
export function recordedThreadReply(): Body {
  const name = slackPayloads().find(
    (candidate) =>
      candidate.endsWith("-event_callback-message") &&
      "thread_ts" in (slackPayload(candidate).event as JsonObject),
  );
  if (name === undefined) throw new Error("no recorded reply in a thread");
  return structuredClone(slackPayload(name));
}

/** The reactions the daemon added to the message at `ts`, in order. */
export function reactionsOn(world: World, ts: string): string[] {
  return world.slack
    .callsTo("reactions.add")
    .filter((args) => args.timestamp === ts)
    .map((args) => String(args.name));
}

/**
 * What the bot posted (not ephemeral), in order; a session's setup message apart, which the
 * setup's own tests read.
 */
export function said(world: World): string[] {
  return world.slack
    .callsTo("chat.postMessage")
    .map((args) => String(args.text))
    .filter((text) => text !== texts.SETUP_FALLBACK);
}

/**
 * A use of a Home tab control: no channel and no message, a `view` container, and the state of
 * the page's controls in `view.state.values` (the block_actions payload reference's Home tab
 * example, docs.slack.dev, read 2026-10-01).
 */
export function homeAction(action: Body, user: string = OWNER, values: Body = {}): Body {
  return {
    type: "block_actions",
    team: { id: TEAM, domain: "example" },
    user: { id: user, username: "alice", name: "alice", team_id: TEAM },
    api_app_id: "A000APP",
    container: { type: "view", view_id: "V000HOME" },
    trigger_id: "1.2.abc",
    view: {
      id: "V000HOME",
      team_id: TEAM,
      type: "home",
      blocks: [],
      state: { values },
    },
    actions: [{ block_id: "b1", action_ts: "1790000000.000001", ...action }],
  };
}

/** A static_select's state, as 001-block_actions.json records its action. */
export function chosenOption(value: string): Body {
  const option = { text: { type: "plain_text", text: value, emoji: true }, value };
  return { type: "static_select", selected_option: option };
}

/** A click on a button of the recorded message, by anyone `user` makes it. */
export function click(actionId: string, value: string, user: Body = {}): Body {
  const body = recorded("block_actions");
  body.actions = [{ ...body.actions[0], action_id: actionId, value }];
  Object.assign(body.user, user);
  return body;
}

/**
 * A click on a Resume button of a picker posted at top level: the value names the session and
 * the thread of the owner's `!resume` message (`threadTs`); the click itself sits at the picker's
 * own ts (CLICK_THREAD).
 */
export function resumeClick(sessionId: string, threadTs: string = THREAD, user: Body = {}): Body {
  return click("session_resume", `${sessionId}@${threadTs}`, user);
}

/**
 * `click`, but on a message posted in `channel`/`threadTs` rather than the fixed CLICK_THREAD
 * fixture: for a button whose message a test itself made the daemon post. `messageTs` is that
 * message's own ts (`removeRequest` deletes it), when it matters to the test; left out, it stays
 * the fixture's own (a click that is refused before it is read).
 */
export function clickIn(
  actionId: string,
  value: string,
  channel: string,
  threadTs: string,
  options: { readonly messageTs?: string; readonly user?: Body } = {},
): Body {
  const body = click(actionId, value, options.user ?? {});
  body.channel.id = channel;
  body.container.thread_ts = threadTs;
  body.message.thread_ts = threadTs;
  if (options.messageTs !== undefined) {
    body.container.message_ts = options.messageTs;
    body.message.ts = options.messageTs;
  }
  return body;
}

/** The value a posted message's own button carries, read back as a click would send it. */
export function buttonValue(blocks: Body[], actionId: string): string {
  for (const block of blocks) {
    for (const element of (block.elements ?? []) as Body[]) {
      if (element.action_id === actionId) return String(element.value);
    }
  }
  throw new assert.AssertionError({ message: `no ${actionId} button in the posted blocks` });
}

/** The questions of the form's tests, as the agent seam gives them (Python's `QUESTIONS`). */
export const QUESTIONS: readonly Question[] = [
  {
    text: "Colour?",
    header: "Colour",
    options: [
      { label: "red", description: null, preview: null },
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

/** The form's recorded Submit (view_submission, 2026-09-24), carrying this draft and state. */
export function formBody(kind: string, draft: Draft, values: Body, user: Body = {}): Body {
  const body = recorded("submit");
  body.type = kind;
  Object.assign(body.user, user);
  body.view.private_metadata = dumpDraft(draft);
  body.view.state = { values };
  return body;
}

export function picked(index: number, value: string): Body {
  return { [`q${index}`]: { answer: { type: "radio_buttons", selected_option: { value } } } };
}

/** Two stored sessions of the channel's folder: one titled, one named by its first prompt. */
export function twoSessions(world: World): void {
  const now = Math.trunc(world.now() * 1000);
  world.storedSessions = [
    // A titled session: its summary is the title.
    listed(SESSION_A, "footer", now - 7_200_000, 412_000, {
      customTitle: "footer",
      branch: "main",
    }),
    // No title: the summary is the first prompt, here long and on several lines.
    listed(SESSION_B, `Trust gate\n${"why ".repeat(60)}`, now - 90_000_000, 1_100_000, {
      branch: "main",
    }),
  ];
}

/** A recorded file_share message (Slack, 2026-09-25), its file changed by `fields`. */
export function sharedFile(kind: string, fields: Body = {}): Body {
  const body = recorded(`event_callback-file_share-${kind}`);
  Object.assign(body.event.files[0], fields);
  return body;
}

/**
 * A message that opens or continues a thread and lets its turn finish, so the session is idle
 * again (a `bind` refuses one that is still busy).
 */
export async function idleMessage(world: World, text: string, ts: string): Promise<void> {
  await world.dispatch(message(text, { ts }));
  const client = world.clients.at(-1);
  if (client === undefined) throw new Error("no client was started");
  client.answer(sdkMessages("tools")); // a full turn, its result included
  const session = world.sessions.get(CHANNEL, ts);
  await world.until(() => session?.idle === true);
}

export function postedBlocks(world: World, index = -1): Body[] {
  const post = world.slack.callsTo("chat.postMessage").at(index);
  if (post === undefined) throw new Error("nothing was posted");
  return post.blocks as Body[];
}

/**
 * The held thread's client is connected (its setup needed the CLI's model list) but nothing was
 * sent on it: only the other session, at `clients[0]`, ever got a prompt.
 */
export function assertHeldUnsent(world: World): void {
  assert.equal(world.clients.length, 2);
  assert.deepEqual(world.clients.at(-1)?.queries, []);
}

/**
 * `otherChannel`'s thread at `OTHER_THREAD` never ends; a top-level message at THREAD, in the
 * same folder, then holds and asks.
 */
export async function startAHold(world: World, otherChannel: string = CHANNEL): Promise<void> {
  if (otherChannel !== CHANNEL) world.state.bind(otherChannel, join(world.root, "app"));
  await world.dispatch(message("busy elsewhere", { ts: OTHER_THREAD, channel: otherChannel }));
  await world.dispatch(message("hello", { ts: THREAD }));
}

export function holdQuestions(world: World): Body[] {
  const prefix = texts.HOLD_QUESTION.split("{")[0] as string;
  return world.slack
    .callsTo("chat.postMessage")
    .filter((post) => String(post.text).startsWith(prefix));
}

/** The id of the hold the last posted message asks about. */
export function lastHoldId(world: World): string {
  return buttonValue(postedBlocks(world), HOLD_CONTINUE);
}

/**
 * `state.values` as Slack sends it for the setup's controls: keyed by block_id, then action_id,
 * and all four controls share one `actions` block (`test/chat/slack/setup.test.ts` names where
 * each shape was recorded).
 */
export function setupState(model?: string, effort?: string, bypass = false): Body {
  const option = (value: string): Body => ({ text: { type: "plain_text", text: value }, value });
  return {
    [SETUP_BLOCK]: {
      [SETUP_MODEL]: { type: "static_select", selected_option: model ? option(model) : null },
      [SETUP_EFFORT]: { type: "static_select", selected_option: effort ? option(effort) : null },
      [SETUP_BYPASS]: { type: "checkboxes", selected_options: bypass ? [option("on")] : [] },
    },
  };
}

/** The elements of the setup's one `actions` block, by action_id. */
export function setupControls(blocks: Body[]): Record<string, Body> {
  const rows = blocks.filter((block) => block.type === "actions");
  assert.equal(rows.length, 1);
  const [row] = rows as [Body];
  assert.equal(row.block_id, SETUP_BLOCK);
  return Object.fromEntries(
    (row.elements as Body[]).map((element) => [element.action_id, element]),
  );
}

/** A click on the (only) waiting setup, carrying `state.values` as Slack would. */
export function setupClick(
  world: World,
  actionId: string = SETUP_START,
  chosen: {
    readonly model?: string;
    readonly effort?: string;
    readonly bypass?: boolean;
    readonly channel?: string;
    readonly threadTs?: string;
    readonly user?: Body;
  } = {},
): Body {
  const waiting = world.waitingSetups();
  assert.equal(waiting.length, 1);
  const [setupId, channel, threadTs, ts] = waiting[0] as [string, string, string, string];
  const body = clickIn(actionId, setupId, chosen.channel ?? channel, chosen.threadTs ?? threadTs, {
    messageTs: ts,
    ...(chosen.user !== undefined && { user: chosen.user }),
  });
  body.state = { values: setupState(chosen.model, chosen.effort, chosen.bypass ?? false) };
  return body;
}

/**
 * Resolve the hold a message shows before `chat.postMessage` has answered, as a fast click does
 * when it arrives ahead of the HTTP answer. Python wrapped the client's `api_call`; here the
 * post's scripted answer does it, since the client binds its methods to `apiCall` when it is
 * made: the hold is resolved once Slack has the message and before the daemon has its ts.
 */
export function answerInsidePost(
  world: World,
  textStarts: string,
  answer: Parameters<Holds["resolve"]>[3],
  action: string,
): void {
  world.slack.responses["chat.postMessage"] = (args) => {
    if (String(args.text ?? "").startsWith(textStarts)) {
      for (const block of (args.blocks ?? []) as Body[]) {
        for (const element of (block.elements ?? []) as Body[]) {
          if (element.action_id === action) {
            world.holds.resolve(element.value, CHANNEL, THREAD, answer);
          }
        }
      }
    }
    // A new ts for each message, as the fake gives one by default.
    const ts = `1790000000.${String(world.slack.createdTs.length + 1).padStart(6, "0")}`;
    return { ...slackPayload("api-chat-postMessage"), ts };
  };
}

/** Hold every `chat.update` behind the process-wide limiter until the event is set. */
export function gateLimiter(world: World, gate: AsyncEvent): void {
  const acquire = world.limiter.acquire.bind(world.limiter);
  world.limiter.acquire = async (signal?: AbortSignal) => {
    await gate.wait();
    await acquire(signal);
  };
}

// --- `!open` ---
// The payloads of the picker's modal follow the Slack reference, read 2026-10-05 on docs.slack.dev:
// block_actions payload (an action from a view: `container.type` "view" with `view_id`, `view.id`,
// `view.hash`, `view.private_metadata`, `view.state.values`, `actions[].action_ts`, and no
// `channel`), view interaction payloads (`view_submission`, and the `response_action` "errors"),
// views.open and views.update (`hash`, optional on an update, and the `hash_conflict` error of a
// hash that is not the view's current one; read again 2026-10-06). The submit is the recorded
// form-submit.json with the picker's view in it.
export const VIEW_ID = "V000PICK";
export const OPEN_TARGET = new Target(CHANNEL, THREAD);

/**
 * Slack's side of one modal as the references describe it: `views.open` answers the view's id
 * and hash, `views.update` answers the new hash and rejects, with `hash_conflict`, a hash that is
 * not the view's current one (the daemon sends none). `view` is the view as it stands, `written`
 * every update that was accepted, `calls` every one that was made, with the hash each carried.
 */
export class ModalSlack {
  hash = "1790000000.h0";
  view: Body = {};
  readonly written: Body[] = [];
  readonly calls: Array<[hash: string | null, view: Body]> = [];
  #count = 0;

  constructor(slack: FakeSlack) {
    slack.responses["views.open"] = (args) => {
      this.view = args.view as Body;
      return { ok: true, view: { id: VIEW_ID, hash: this.#next() } };
    };
    slack.responses["views.update"] = (args) => {
      const hash = typeof args.hash === "string" ? args.hash : null;
      this.calls.push([hash, args.view as Body]);
      if ((hash !== null && hash !== this.hash) || args.view_id !== VIEW_ID) {
        return rejected("hash_conflict");
      }
      this.view = args.view as Body;
      this.written.push(this.view);
      return { ok: true, view: { id: VIEW_ID, hash: this.#next() } };
    };
  }

  #next(): string {
    this.#count += 1;
    this.hash = `1790000000.h${this.#count}`;
    return this.hash;
  }

  /** What the view says about its rows: the radio group's label, else its context lines. */
  labels(): string[] {
    return (this.view.blocks as Body[])
      .slice(1)
      .map((block) => (block.type === "input" ? block.label.text : block.elements[0].text));
  }

  /** The paths of the radio group's rows, in order. */
  rows(view: Body = this.view): string[] {
    for (const block of view.blocks as Body[]) {
      if (String(block.block_id ?? "").startsWith(CHOICE_BLOCK)) {
        return (block.element.options as Body[]).map((option) => option.value);
      }
    }
    return [];
  }
}

/** The files the bot shared, as `files.completeUploadExternal` was asked to. */
export function opened(world: World): Body[] {
  return world.slack.callsTo("files.completeUploadExternal");
}

/** The messages the bot posted that hold the button of `!open`'s modal. */
export function pickerPosts(world: World): Body[] {
  return world.slack
    .callsTo("chat.postMessage")
    .filter((post) =>
      ((post.blocks ?? []) as Body[]).some((block) =>
        ((block.elements ?? []) as Body[]).some(
          (element) => element.action_id === OPEN_BUTTON_ACTION,
        ),
      ),
    );
}

/**
 * A repository with one commit, `README`, made long before THREAD began: what a session started
 * in an existing repository sees. (A repository made after the thread began counts every file as
 * changed.)
 */
export function madeBeforeTheThread(path: string): string {
  gitInit(path);
  commitAt(path, "README", Number(THREAD.split(".")[0]) - 1000);
  return realpathSync(path);
}

/** The `project` fixture: the channel's folder as a repository with one commit, `README`. */
export function projectOf(world: World): string {
  return madeBeforeTheThread(join(world.root, "app"));
}

export function put(root: string, name: string, text = "x\n"): string {
  const path = join(root, name);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, text);
  return path;
}

/**
 * A click on the `Choose a file` button of a message in THREAD: the recorded button click
 * (`000-block_actions.json` shape), with the `trigger_id` a real click carries.
 */
export function chooseClick(words = "", user: Body = {}): Body {
  const body = clickIn(OPEN_BUTTON_ACTION, words, CHANNEL, THREAD, { user });
  delete body.actions[0].selected_option;
  body.actions[0].type = "button";
  if (!words) delete body.actions[0].value;
  body.trigger_id = "0000000000.0000000000.fake";
  return body;
}

/**
 * What Slack sends for the picker's modal: `view_submission` is the recorded Submit with the
 * picker's view in it, `block_actions` is built from the reference (no recording of one from a
 * modal exists).
 */
export function viewEvent(
  kind: "view_submission" | "block_actions",
  values: Body,
  options: {
    readonly metadata?: string;
    readonly user?: string;
    readonly team?: string;
    readonly viewHash?: string;
    readonly blocks?: Body[];
  } = {},
): Body {
  const view = {
    id: VIEW_ID,
    team_id: TEAM,
    type: "modal",
    blocks: options.blocks ?? modalView(OPEN_TARGET, "", ["a.py"]).blocks,
    private_metadata: options.metadata ?? OPEN_TARGET.dump(),
    callback_id: OPEN_FORM,
    state: { values },
    hash: options.viewHash ?? "1790000000.h0",
  };
  let body: Body;
  if (kind === "view_submission") {
    body = recorded("submit");
    Object.assign(body.view, view);
  } else {
    body = {
      type: "block_actions",
      team: { id: TEAM, domain: "example" },
      user: { id: OWNER, username: "alice", name: "alice", team_id: TEAM },
      api_app_id: "A000APP",
      container: { type: "view", view_id: VIEW_ID },
      trigger_id: "1.2.abc",
      view,
    };
  }
  body.user.id = options.user ?? OWNER;
  body.team.id = options.team ?? TEAM;
  return body;
}

/**
 * One character typed in the search field (`on_character_entered`): the action carries the text
 * and its `action_ts`, and the view's state carries the same text.
 */
export function typing(
  text: string,
  at = 1790000100.0,
  options: Parameters<typeof viewEvent>[2] = {},
): Body {
  const values = { [QUERY_BLOCK]: { [QUERY_ACTION]: { type: "plain_text_input", value: text } } };
  const body = viewEvent("block_actions", values, options);
  body.actions = [
    {
      type: "plain_text_input",
      block_id: QUERY_BLOCK,
      action_id: QUERY_ACTION,
      value: text,
      action_ts: at.toFixed(6),
    },
  ];
  return body;
}

export function shownChoiceId(blocks: Body[]): string {
  const found = blocks.find((block) => String(block.block_id ?? "").startsWith(CHOICE_BLOCK));
  if (found === undefined) throw new Error("the view shows no rows");
  return found.block_id;
}

/**
 * The Submit of the picker's modal with `path` chosen (a radio group's state as form-submit.json
 * records it), or nothing chosen. The view holds `rows` (just `path` by default); the choice is in
 * the state of the block `under` (the rows' own block by default: Slack keeps the state of a block
 * id from one update to the next).
 */
export function submitted(
  path: string | null,
  options: Parameters<typeof viewEvent>[2] & {
    readonly rows?: string[];
    readonly under?: string;
  } = {},
): Body {
  const { rows, under, ...event } = options;
  const shown = rows ?? (path === null ? [] : [path]);
  const blocks = (event.blocks ?? modalView(OPEN_TARGET, "", shown).blocks) as Body[];
  const option = path === null ? null : { text: { type: "plain_text", text: path }, value: path };
  const blockId =
    under ??
    blocks.find((block) => String(block.block_id ?? "").startsWith(CHOICE_BLOCK))?.block_id;
  const values: Body = {
    [QUERY_BLOCK]: { [QUERY_ACTION]: { type: "plain_text_input", value: null } },
  };
  if (blockId !== undefined) {
    values[blockId] = { [CHOICE_ACTION]: { type: "radio_buttons", selected_option: option } };
  }
  return viewEvent("view_submission", values, { ...event, blocks });
}

/** `text` (an `!open` word) sent in THREAD, and the time its git commands need. */
export async function askedOpen(world: World, text: string): Promise<void> {
  await world.dispatch(reply(text, THREAD));
  await world.settle();
}

/** A session in THREAD, whose first sight is now (its start commit). */
export async function inAThread(world: World): Promise<void> {
  await world.dispatch(message("hello", { ts: THREAD }));
}

// --- An audio clip as a prompt (issue #35) ---
// `file_share-clip` is the recorded file_share envelope with the file object Slack returned for a
// clip on 2026-10-09 (`conversations.history`, ids and text made synthetic); the event Slack sends
// when a clip is posted was not recorded, so the envelope is the snippet's. `file_change` follows
// the event reference (read 2026-10-09): the daemon's log names the event, not its body.

export function clipMessage(transcribed: boolean, fields: Body = {}): Body {
  const body = sharedFile("clip", fields);
  if (!transcribed) delete body.event.files[0].transcription;
  return body;
}

/** What `files.info` answers for the clip from now on. */
export function clipInfo(world: World, fields: Body = {}): Body {
  const file = { ...sharedFile("clip").event.files[0], ...fields };
  world.slack.responses["files.info"] = { ok: true, file };
  return file;
}

export function ownerWasTold(world: World): string[] {
  return world.slack.callsTo("chat.postEphemeral").map((call) => String(call.text));
}
