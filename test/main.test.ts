/**
 * The entry point, run whole on fakes: `run` over `FakeSlack`, the scripted agent back end of the
 * sessions harness, a fake clock, an emitter for the signals and a receiver that opens no socket.
 * Port of `tests/test_main.py`. Python waited on `asyncio.wait_for(run, timeout=1)` for a daemon
 * that never stops by itself; here a test waits for the receiver's start (the connection) and
 * stops the daemon with a signal. Nothing waits on the wall clock: the Home tab's debounce and
 * the cleanup's schedule are crossed by advancing `FakeClock`.
 *
 * Not ported, with the reason (the six tests of `_alive_sessions` and `_projects_dir`, which
 * moved to the Claude back end as `ClaudeBackend.aliveSessions`, and were ported where the
 * function now lives, `test/agent/claude/listing.test.ts` and `backend.test.ts`):
 * - `test_alive_sessions_reads_directory_sessions`: "the ids alive are the listed sessions and
 *   every transcript file there".
 * - `test_alive_sessions_keeps_a_transcript_list_sessions_filters_out`: the same test.
 * - `test_alive_sessions_cannot_tell_about_a_folder_it_cannot_read`: "a transcripts folder that
 *   cannot be read cannot tell".
 * - `test_alive_sessions_cannot_decide_a_long_folder_it_does_not_find`: the test of the long
 *   folder name's key, in the same file.
 * - `test_alive_sessions_decides_a_short_folder_it_does_not_find`: "a folder with no transcripts
 *   folder has the listing alone".
 * What `main.ts` owns of them, that the cleanup is given the back end's function, is the test
 * `prune uses alive sessions to drop a gone thread`.
 *
 * Beyond the port, from the critical review of 2026-10-10: the daemon is built over one fake for
 * each Slack client it makes (`slack`, `replies`, `owner`), so a part wired to the wrong client or
 * token fails a test; a listener still in flight when the stop ends is held on the channel check
 * and let go after `run` returned; and what the process does with an exception or a rejection
 * nobody handled, and with an event loop that something still holds after the stop.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { type TestContext, test } from "node:test";
import type { App } from "@slack/bolt";
import { type FetchFunction, LogLevel, WebAPIRequestError } from "@slack/web-api";
import type { BuildAppOptions } from "../src/chat/slack/app/app.ts";
import { DELETE_ACTION, EDIT_ACTION, EDIT_ON } from "../src/chat/slack/home.ts";
import type { Clock } from "../src/clock.ts";
import { CLEAN_EVERY_SECONDS, logger as cleanupLogger } from "../src/core/cleanup.ts";
import { ConfigError } from "../src/core/config.ts";
import { AlreadyRunning, singleInstance } from "../src/core/lock.ts";
import { StateStore } from "../src/core/state.ts";
import * as texts from "../src/core/texts.ts";
import {
  cleanEvery,
  DRAIN_LIMIT_SECONDS,
  deferred,
  deleter,
  EXIT_GRACE_SECONDS,
  FAILURE_STOP_SECONDS,
  installProcessHandlers,
  logger,
  main,
  makeClients,
  postUpgradeNotices,
  type RunOptions,
  run,
  type SignalSource,
  type Stop,
  StopSignals,
} from "../src/main.ts";
import {
  AsyncEvent,
  BOT,
  CHANNEL,
  FakeClock,
  FakeSlack,
  OWNER,
  rejected,
  TEAM,
} from "./support/fake-slack.ts";
import { slackPayload } from "./support/fixtures.ts";
import { FakeAgentBackend, realWorkDone, type Script, sdkMessages } from "./support/sessions.ts";
import { homeAction, reply } from "./support/slack-app.ts";

const ROOT = join(import.meta.dirname, "..");
const GONE = "C000GONE";
const BOT_TOKEN = "xox" + "b-1";
const APP_TOKEN = "xap" + "p-1";
const USER_TOKEN = "xox" + "p-1";

/** A configuration directory with a valid `.env`, as the owner's would be (mode 600). */
function configDirectory(t: TestContext, options: { readonly userToken?: boolean } = {}): string {
  const dir = mkdtempSync(join(tmpdir(), "awaydesk-main-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const env = join(dir, ".env");
  writeFileSync(
    env,
    `SLACK_BOT_TOKEN=${BOT_TOKEN}\nSLACK_APP_TOKEN=${APP_TOKEN}\n` +
      `SLACK_OWNER_USER_ID=${OWNER}\nALLOWED_ROOT=${dir}\n` +
      (options.userToken === true ? `SLACK_USER_TOKEN=${USER_TOKEN}\n` : ""),
  );
  chmodSync(env, 0o600);
  return dir;
}

/** The first lines of the section blocks of every page published to the Home tab, in order. */
function firstLines(slack: FakeSlack): string[] {
  return slack
    .callsTo("views.publish")
    .flatMap((args) =>
      (
        ((args.view as { blocks: Array<Record<string, unknown>> }).blocks ?? []) as Array<
          Record<string, unknown>
        >
      )
        .filter((block) => block.type === "section" && !("accessory" in block))
        .map((block) => String((block.text as { text: string }).text.split("\n")[0])),
    );
}

/** A v1 `state.json`, migrated on load: each of its channels gets a pending notice. */
function v1State(dir: string, channel: string = CHANNEL): StateStore {
  const path = join(dir, "state.json");
  writeFileSync(path, JSON.stringify({ version: 1, channels: { [channel]: { directory: dir } } }));
  return new StateStore(path);
}

/**
 * The daemon over fakes. One fake for each client it builds, so a test can tell which one a call
 * went through: `slack` (the bot's shared client), `replies` (the bot's client for replies) and
 * `owner` (the user token's).
 */
class Daemon {
  readonly slack = new FakeSlack();
  readonly replies = new FakeSlack();
  readonly owner = new FakeSlack();
  readonly clock = new FakeClock();
  readonly backend: FakeAgentBackend;
  readonly signals = new EventEmitter();
  readonly connected = new AsyncEvent();
  /** What the receiver was asked, in order, for the tests of an order. */
  readonly events: string[] = [];
  /** The token each client constructor was given, in order: `[constructor, token]`. */
  readonly tokens: Array<readonly [string, string]> = [];
  /** The app the receiver was given, once built: where a test delivers a payload from. */
  app: App | null = null;
  readonly dir: string;
  running: Promise<number> | null = null;
  startFails: Error | null = null;

  constructor(t: TestContext, options: { scripts?: Script[]; userToken?: boolean } = {}) {
    this.dir = configDirectory(t, options);
    this.backend = new FakeAgentBackend(options.scripts ?? []);
    this.signals.setMaxListeners(0);
    t.after(async () => {
      // A test that failed must not leave the daemon (and its timer) running.
      if (this.running !== null) {
        this.signals.emit("SIGINT");
        await this.running.catch(() => undefined);
      }
    });
  }

  get path(): string {
    return join(this.dir, "state.json");
  }

  readonly receiver: BuildAppOptions["receiver"] = {
    init: (app) => {
      this.app = app;
    },
    start: async () => {
      this.events.push("connect");
      if (this.startFails !== null) throw this.startFails;
      this.connected.set();
    },
    stop: async () => {
      this.events.push("app.stop");
    },
  };

  /** A payload as the socket would deliver it; settles once every listener of it has ended. */
  deliver(body: Record<string, unknown>): Promise<void> {
    assert.ok(this.app !== null, "the receiver was not given the app");
    return this.app.processEvent({ body, ack: async () => {} });
  }

  options(extra: RunOptions = {}): RunOptions {
    return {
      configDir: this.dir,
      clients: {
        bot: (token) => {
          this.tokens.push(["bot", token]);
          return [this.slack, this.replies];
        },
        owner: (token) => {
          this.tokens.push(["owner", token]);
          return this.owner;
        },
      },
      receiver: (token) => {
        this.tokens.push(["receiver", token]);
        return this.receiver;
      },
      backend: this.backend,
      // The usage probe's session: never the real Claude Code, whatever a test runs.
      bareStart: async () => {
        throw new Error("no usage in this test");
      },
      clock: this.clock,
      signals: this.signals as SignalSource,
      uploads: join(this.dir, "uploads"),
      ...extra,
    };
  }

  start(extra: RunOptions = {}): Promise<number> {
    this.running = run(this.options(extra));
    return this.running;
  }

  /** Resolves once Socket Mode connected; fails the test if `run` ended before. */
  async booted(): Promise<void> {
    const running = this.running;
    assert.ok(running !== null, "the daemon was not started");
    await Promise.race([
      this.connected.wait(),
      running.then(() => assert.fail("run ended before it connected")),
    ]);
  }

  async stop(signal: Stop = "SIGTERM"): Promise<number> {
    assert.ok(this.running !== null, "the daemon was not started");
    this.signals.emit(signal);
    return this.running;
  }

  /**
   * Advances the clock a second at a time until `done` holds, at most a hundred times. Each step
   * waits for the file calls it set going, so a loaded machine does not use the steps up.
   */
  async until(done: () => boolean): Promise<void> {
    for (let round = 0; round < 100; round += 1) {
      if (done()) return;
      await this.clock.advance(1);
      await realWorkDone();
    }
    assert.ok(done(), "the daemon never reached the state the test waited for");
  }
}

function tick(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

/** What a logger was asked to write while the test ran. */
function written(
  t: TestContext,
  target: { [K in "info" | "warning" | "error"]: unknown },
  level: "info" | "warning" | "error",
) {
  const spy = t.mock.method(target as Record<string, (message: string) => void>, level, () => {});
  return () => spy.mock.calls.map((call) => String(call.arguments[0]));
}

test("a bad config exits 1 with the reason", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "awaydesk-main-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const errors = written(t, logger, "error");
  const exits: number[] = [];
  await main({ configDir: dir, exit: (code) => exits.push(code), process: new EventEmitter() });
  assert.deepEqual(exits, [1]);
  assert.ok(errors().some((line) => line.includes("docs/setup.md")));
});

test("a second instance stops before slack", async (t) => {
  const dir = configDirectory(t);
  const unreached: RunOptions = {
    configDir: dir,
    clients: {
      bot: () => assert.fail("Slack was reached"),
      owner: () => assert.fail("Slack was reached"),
    },
  };
  const held = await singleInstance(dir);
  try {
    await assert.rejects(run(unreached), AlreadyRunning);
  } finally {
    await held.release();
  }
});

test("run prunes stale threads and survives alive raising", async (t) => {
  const d = new Daemon(t);
  // A bound channel with a stored session id: `run` must ask the back end about it before
  // connecting, and a broken transcript read there must not stop the daemon from starting.
  const state = new StateStore(d.path);
  state.bind(CHANNEL, d.dir);
  state.openThread(CHANNEL, "1780000000.000001", "some-id");
  t.mock.method(d.backend, "aliveSessions", async () => {
    throw new Error("transcripts unreadable");
  });
  const warnings = written(t, cleanupLogger, "warning");

  d.start();
  await d.booted();
  assert.ok(warnings().some((line) => line.includes("could not prune stale threads")));
  await d.stop();
});

test("run repairs a crashed thread before pruning it", async (t) => {
  // A thread with no session id, its root message older than a day (`state.ONE_DAY`): prune
  // drops it. Repair must still act on it first (issue #19 ruling 5), since it is exactly what
  // a crash mid-first-turn leaves.
  const d = new Daemon(t);
  const state = new StateStore(d.path);
  state.bind(CHANNEL, d.dir);
  state.openThread(CHANNEL, "1000000000.000001");
  state.setStatusPending(CHANNEL, "1000000000.000001", "raised_hand");

  d.start();
  await d.booted();
  const added = d.slack.callsTo("reactions.add").map((args) => args.name);
  assert.deepEqual(added, ["x"]);
  // Repaired, then pruned: nothing left of the thread at all.
  assert.equal(new StateStore(d.path).thread(CHANNEL, "1000000000.000001"), null);
  await d.stop();
});

test("run publishes the session index once started and after a change", async (t) => {
  const d = new Daemon(t);
  const threadTs = "1780000000.000001";
  const state = new StateStore(d.path);
  state.bind(CHANNEL, d.dir);
  state.openThread(CHANNEL, threadTs, "some-id");
  state.setStatusPending(CHANNEL, threadTs, "hourglass_flowing_sand"); // a crash left it

  const nowSeconds = Date.now() / 1000;
  d.clock.now = nowSeconds; // inside the 48 hours the page starts on
  d.backend.listed = [
    {
      id: "some-id",
      title: "Fix the footer",
      customTitle: null,
      branch: null,
      size: null,
      lastModified: nowSeconds * 1000,
    },
  ];
  // The thread's root as Slack returns it, its last reply now: inside the page's 48 hours.
  const recorded = (slackPayload("api-conversations-replies-root").messages as object[])[0];
  const root = {
    ...recorded,
    ts: threadTs,
    thread_ts: threadTs,
    latest_reply: nowSeconds.toFixed(6),
    reactions: [],
  };
  d.slack.responses["conversations.replies"] = { ok: true, messages: [root] };
  const stores: StateStore[] = [];

  d.start({
    openState: (path) => {
      stores.push(new StateStore(path));
      return stores[0] as StateStore;
    },
  });
  await d.booted();
  // Published after the repair: the root a crash left ⏳ already reads ❌.
  await d.until(() => firstLines(d.slack).join() === ":x:  *Fix the footer*");
  assert.equal(d.slack.callsTo("views.publish")[0]?.user_id, OWNER);
  (stores[0] as StateStore).setStatusPending(CHANNEL, threadTs, "raised_hand");
  await d.until(() => firstLines(d.slack).at(-1) === ":raised_hand:  *Fix the footer*");
  await d.stop();
});

test("run forgets a gone channel at start and again on its schedule", async (t) => {
  const d = new Daemon(t);
  const state = v1State(d.dir, GONE); // a channel still owing its upgrade notice
  state.bind(CHANNEL, d.dir);
  state.bind("C000LATE", d.dir);
  const gone = new Set([GONE]);
  d.slack.responses["conversations.info"] = (args) =>
    gone.has(String(args.channel))
      ? rejected("channel_not_found")
      : slackPayload("api-conversations-info");
  let notices: ReturnType<typeof t.mock.method> | null = null;
  const bound = (): string[] =>
    Object.keys((JSON.parse(readFileSync(d.path, "utf8")) as { channels: object }).channels);

  d.start({
    openState: (path) => {
      const store = new StateStore(path);
      notices = t.mock.method(store, "pendingNotices");
      return store;
    },
  });
  await d.booted();
  await tick();
  assert.deepEqual(bound(), [CHANNEL, "C000LATE"]);
  // The notices were looked for, and the forgotten channel was not tried: nothing is posted.
  assert.ok((notices as unknown as { mock: { calls: unknown[] } }).mock.calls.length > 0);
  assert.deepEqual(
    d.slack.callsTo("chat.postMessage").map((args) => args.channel),
    [],
  );
  gone.add("C000LATE"); // deleted while the daemon runs
  await d.clock.advance(CLEAN_EVERY_SECONDS);
  await d.until(() => bound().join() === CHANNEL);
  // SIGINT: no drain, so only the end of the schedule itself keeps a later pass from running.
  await d.stop("SIGINT");
  // The schedule ends with the daemon: no pass asks Slack about a channel after the stop.
  const asked = d.slack.callsTo("conversations.info").length;
  await d.clock.advance(CLEAN_EVERY_SECONDS * 2);
  assert.equal(d.slack.callsTo("conversations.info").length, asked);
});

test("the scheduled cleanup stops once a stop has begun", async () => {
  const passes: string[] = [];
  const clock = new FakeClock();
  const done = cleanEvery({
    seconds: 0.01,
    clean: async () => {
      passes.push("clean");
    },
    sessions: { draining: true },
    clock,
    signal: new AbortController().signal,
  });
  await clock.advance(1);
  await done;
  assert.deepEqual(passes, []);
});

test("repair and prune run before the socket mode connection opens", async (t) => {
  // The exact order: repair, then prune, then the connection. The repair is seen at its
  // reaction on the root a crash left raised, the prune at the state's own method.
  const d = new Daemon(t);
  const state = new StateStore(d.path);
  state.bind(CHANNEL, d.dir);
  state.openThread(CHANNEL, "1000000000.000001");
  state.setStatusPending(CHANNEL, "1000000000.000001", "raised_hand");
  const order: string[] = [];
  d.slack.responses["reactions.add"] = () => {
    order.push("repair");
    return { ok: true };
  };
  const receiverStart = d.receiver.start.bind(d.receiver);
  t.mock.method(d.receiver, "start", async () => {
    order.push("connect");
    await receiverStart();
  });

  d.start({
    openState: (path) => {
      const store = new StateStore(path);
      const prune = store.prune.bind(store);
      t.mock.method(store, "prune", (...args: Parameters<StateStore["prune"]>) => {
        order.push("prune");
        return prune(...args);
      });
      return store;
    },
  });
  await d.booted();
  assert.deepEqual(order, ["repair", "prune", "connect"]);
  await d.stop();
});

test("signal handlers are installed before a long repair runs", async (t) => {
  // Installed before repair, not after, so a SIGTERM arriving during a long repair is caught
  // instead of ending the process under the default disposition, which would skip every step of
  // the shutdown (the single-instance lock, Socket Mode, every session).
  const d = new Daemon(t);
  const state = new StateStore(d.path);
  state.bind(CHANNEL, d.dir);
  state.openThread(CHANNEL, "1000000000.000001");
  state.setStatusPending(CHANNEL, "1000000000.000001", "raised_hand");
  const order: string[] = [];
  d.slack.responses["reactions.add"] = () => {
    order.push("repair");
    return { ok: true };
  };
  const recording: SignalSource = {
    on: (signal, listener) => {
      order.push("signal");
      d.signals.on(signal, listener);
    },
    off: (signal, listener) => d.signals.off(signal, listener),
  };

  d.start({ signals: recording });
  await d.booted();
  assert.deepEqual(order, ["signal", "signal", "repair"]); // SIGTERM and SIGINT, both before repair
  await d.stop();
});

test("no name carries claude code", () => {
  const manifest = JSON.parse(readFileSync(join(ROOT, "slack-app-manifest.json"), "utf8"));
  const names: string[] = [
    manifest.display_information.name,
    manifest.features.bot_user.display_name,
  ];
  const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"));
  assert.ok(names.every((name) => !name.toLowerCase().includes("claude code")));
  assert.equal(pkg.name, "awaydesk");
});

test("the manifest asks for the minimum", () => {
  const manifest = JSON.parse(readFileSync(join(ROOT, "slack-app-manifest.json"), "utf8"));
  assert.deepEqual([...manifest.oauth_config.scopes.bot].sort(), [
    "chat:write",
    "files:read", // downloading the files attached to a message (the maintainer, 2026-09-25)
    "files:write", // `!open` shares a file of the session's folder into its thread
    "groups:history",
    "groups:read",
    "reactions:write", // the status reaction on a session's root message
  ]);
  assert.deepEqual(manifest.settings.event_subscriptions.bot_events, [
    "message.groups",
    // Slack sends it while it writes a clip's transcript, the only sign that one is ready
    // (measured 2026-10-09); read under `files:read`, with no scope of its own.
    "file_change",
  ]);
  assert.equal(manifest.settings.is_mcp_enabled, false);
  // The session index is the app's Home tab: published with no scope and no event
  // (views.publish reference, read 2026-10-01). Nobody writes to the app in its Messages tab.
  assert.deepEqual(manifest.features.app_home, {
    home_tab_enabled: true,
    messages_tab_enabled: true,
    messages_tab_read_only_enabled: true,
  });
  // Replies are plain messages in the main window: no agent view, no task cards.
  assert.ok(!("agent_view" in manifest.features));
  // Commands are typed as `!word` messages: the app registers no slash command.
  assert.ok(!("slash_commands" in manifest.features));
});

test("the upgrade notice posts top level and clears the flag", async (t) => {
  const state = v1State(configDirectory(t));
  assert.deepEqual(state.pendingNotices(), [CHANNEL]);
  const slack = new FakeSlack();
  await postUpgradeNotices(slack, state);
  const [post, ...others] = slack.callsTo("chat.postMessage");
  assert.deepEqual(others, []);
  assert.equal(post?.channel, CHANNEL);
  assert.equal(post?.thread_ts, undefined); // not a reply to any message
  assert.equal(post?.text, texts.UPGRADE_NOTICE);
  assert.deepEqual(state.pendingNotices(), []);
});

test("a failed upgrade notice keeps the flag for next start", async (t) => {
  const state = v1State(configDirectory(t));
  const slack = new FakeSlack();
  slack.responses["chat.postMessage"] = new Error("network down");
  await postUpgradeNotices(slack, state);
  assert.deepEqual(state.pendingNotices(), [CHANNEL]);
});

test("prune uses alive sessions to drop a gone thread", async (t) => {
  // The cleanup is given the back end's `aliveSessions`: a thread whose session the back end no
  // longer lists is dropped at start, one it still lists is kept.
  const d = new Daemon(t);
  const state = new StateStore(d.path);
  state.bind(CHANNEL, d.dir);
  state.openThread(CHANNEL, "1780000000.000001", "gone");
  state.openThread(CHANNEL, "1780000000.000002", "kept");
  d.backend.listed = [
    { id: "kept", title: "kept", customTitle: null, branch: null, size: null, lastModified: 1 },
  ];

  d.start();
  await d.booted();
  const after = new StateStore(d.path);
  assert.equal(after.thread(CHANNEL, "1780000000.000001"), null);
  assert.notEqual(after.thread(CHANNEL, "1780000000.000002"), null);
  await d.stop();
});

test("the user token must be the owner s own in the bot s workspace", async (t) => {
  const identity = { ownerUserId: OWNER, teamId: TEAM, botUserId: BOT };
  const state = new StateStore(join(configDirectory(t), "state.json"));
  const sessions = { release: async () => true, free: () => {} };
  const bot = new FakeSlack();
  // No token: nothing deletes, and Slack is not asked anything.
  assert.equal(await deleter(null, bot, identity, state, sessions), null);
  assert.deepEqual(bot.apiCalls, []);

  const owner = new FakeSlack();
  const clients = { owner: () => owner };
  const token = "xox" + "p-fake";
  // `auth.test` names who a token acts as (api-auth-test.json has the same fields for the bot).
  owner.responses["auth.test"] = { ok: true, team_id: TEAM, user_id: OWNER };
  assert.notEqual(await deleter(token, bot, identity, state, sessions, clients), null);
  for (const who of [
    { team_id: TEAM, user_id: "U000BOB" },
    { team_id: "T000OTHER", user_id: OWNER },
  ]) {
    owner.responses["auth.test"] = { ok: true, ...who };
    await assert.rejects(
      deleter(token, bot, identity, state, sessions, clients),
      (error: unknown) => error instanceof ConfigError && /SLACK_USER_TOKEN/.test(error.message),
    );
  }
});

test("only the reply client skips the connection retry of a post", async () => {
  // A reset before an answer, then Slack answering: what a retry would find.
  const resetThenAnswer = (requests: string[]): FetchFunction => {
    return async (url) => {
      requests.push(String(url));
      if (requests.length === 1) {
        const cause = Object.assign(new Error("read ECONNRESET"), { code: "ECONNRESET" });
        throw new TypeError("fetch failed", { cause });
      }
      return {
        ok: true,
        status: 200,
        statusText: "OK",
        url: String(url),
        headers: { get: () => null, entries: () => [] },
        arrayBuffer: async () => new ArrayBuffer(0),
        json: async () => ({ ok: true }),
        text: async () => JSON.stringify({ ok: true }),
      };
    };
  };
  // The pauses before a retry end at once.
  const instant: Clock = { sleep: async () => {}, time: () => 0 };
  const sent = async (which: 0 | 1): Promise<string[] | WebAPIRequestError> => {
    const requests: string[] = [];
    const clients = makeClients("xoxb-" + "fake", {
      fetch: resetThenAnswer(requests),
      clock: instant,
      logLevel: LogLevel.ERROR,
      random: () => 0,
    });
    const client = clients[which];
    try {
      await client.chat.postMessage({ channel: CHANNEL, text: "an approval" });
    } catch (error) {
      if (error instanceof WebAPIRequestError) return error;
      throw error;
    }
    return requests;
  };
  // An approval or a question posted through the shared client is still retried after a reset.
  assert.equal((await sent(0)) instanceof WebAPIRequestError, false);
  assert.equal(((await sent(0)) as string[]).length, 2);
  assert.equal((await sent(1)) instanceof WebAPIRequestError, true);
});

// What `__main__.py` did without a test of its own: the stop, the process and the entry point.

test("a stop by SIGTERM lets running turns finish and SIGINT does not", async (t) => {
  const infos = written(t, logger, "info");
  const d = new Daemon(t);
  d.start();
  await d.booted();
  await d.stop("SIGTERM");
  assert.deepEqual(
    infos().filter((line) => line.startsWith("stopping") || line === "shutting down"),
    ["stopping: letting running turns finish", "shutting down"],
  );

  const second = new Daemon(t);
  const again = written(t, logger, "info");
  second.start();
  await second.booted();
  await second.stop("SIGINT");
  assert.deepEqual(
    again().filter((line) => line.startsWith("stopping") || line === "shutting down"),
    ["shutting down"],
  );
});

test("a stop closes the connection before the last page of the session index", async (t) => {
  const d = new Daemon(t);
  d.slack.responses["views.publish"] = () => {
    d.events.push("page");
    return { ok: true };
  };
  d.start();
  await d.booted();
  // The first page is still owed (its debounce has not passed): the stop publishes it, last.
  await d.stop();
  assert.deepEqual(d.events, ["connect", "app.stop", "page"]);
});

test("the lock is released when the daemon has stopped", async (t) => {
  const d = new Daemon(t);
  d.start();
  await d.booted();
  await assert.rejects(singleInstance(d.dir), AlreadyRunning);
  await d.stop();
  await (await singleInstance(d.dir)).release();
});

test("a signal during the repair stops the daemon once it is up", async (t) => {
  const d = new Daemon(t);
  const state = new StateStore(d.path);
  state.bind(CHANNEL, d.dir);
  state.openThread(CHANNEL, "1000000000.000001");
  state.setStatusPending(CHANNEL, "1000000000.000001", "raised_hand");
  d.slack.responses["reactions.add"] = () => {
    d.signals.emit("SIGTERM"); // while the repair runs
    return { ok: true };
  };
  await d.start();
  assert.deepEqual(d.events, ["connect", "app.stop"]);
});

test("a start that fails still shuts down", async (t) => {
  const d = new Daemon(t);
  d.startFails = new Error("invalid_auth");
  const before = process.getActiveResourcesInfo().filter((name) => name === "Timeout").length;
  await assert.rejects(d.start(), /invalid_auth/);
  // The connection is closed, the cleanup's schedule ended, the lock released.
  assert.deepEqual(d.events, ["connect", "app.stop"]);
  assert.equal(
    process.getActiveResourcesInfo().filter((name) => name === "Timeout").length,
    before,
  );
  await (await singleInstance(d.dir)).release();
});

test("nothing is left to keep the process alive after a stop", async (t) => {
  const timers = () => process.getActiveResourcesInfo().filter((name) => name === "Timeout").length;
  const d = new Daemon(t);
  const before = timers();
  d.start();
  await d.booted();
  assert.ok(timers() > before, "the daemon holds a timer of its own while it runs");
  await d.stop();
  assert.equal(timers(), before);
  assert.equal(d.signals.listenerCount("SIGTERM"), 0);
  assert.equal(d.signals.listenerCount("SIGINT"), 0);
});

test("a stop by a signal exits without a code", async (t) => {
  const d = new Daemon(t);
  const exits: number[] = [];
  const running = main({
    ...d.options(),
    exit: (code) => exits.push(code),
    process: new EventEmitter(),
    later: () => {},
  });
  await d.connected.wait();
  d.signals.emit("SIGTERM");
  await running;
  assert.deepEqual(exits, []);
});

// The end of the process (F1 part c): `run` returned, yet something may still hold the event
// loop (a Claude Code child, a socket), and launchd would see a live job and start nothing.

/** `main` over the daemon, with a `later` that keeps what it was asked instead of arming a timer. */
function mainOf(d: Daemon, events: EventEmitter = new EventEmitter()) {
  const exits: number[] = [];
  const timers: Array<{ seconds: number; action: () => void }> = [];
  const running = main({
    ...d.options(),
    exit: (code) => exits.push(code),
    process: events,
    later: (seconds, action) => {
      timers.push({ seconds, action });
    },
  });
  return { exits, timers, running, events };
}

test("a stop arms a timer that ends the process if the event loop is still held", async (t) => {
  const d = new Daemon(t);
  const { exits, timers, running } = mainOf(d);
  const warnings = written(t, logger, "warning");
  await d.connected.wait();
  d.signals.emit("SIGTERM");
  await running;
  // Armed after `run` returned, not before: the exit comes from nothing but the grace.
  assert.deepEqual(exits, []);
  assert.deepEqual(
    timers.map((timer) => timer.seconds),
    [EXIT_GRACE_SECONDS],
  );
  assert.deepEqual(warnings(), []);
  timers[0]?.action();
  assert.deepEqual(exits, [0]);
  assert.equal(warnings().length, 1);
  assert.match(warnings()[0] ?? "", /did not end by itself/);
});

test("the timer that ends the process does not keep it alive", () => {
  const timer = deferred(60, () => {});
  try {
    assert.equal(timer.hasRef(), false);
  } finally {
    clearTimeout(timer);
  }
});

test("the grace before the process is ended is a few seconds", () => {
  assert.ok(EXIT_GRACE_SECONDS >= 1 && EXIT_GRACE_SECONDS <= 10);
});

test("an unexpected error ends with 1 and logs only its name", async (t) => {
  const d = new Daemon(t);
  d.slack.responses["auth.test"] = rejected("invalid_auth", { detail: "xoxb-secret" });
  const errors = written(t, logger, "error");
  const exits: number[] = [];
  await main({ ...d.options(), exit: (code) => exits.push(code), process: new EventEmitter() });
  assert.deepEqual(exits, [1]);
  assert.deepEqual(errors(), ["stopped by an unexpected error: invalid_auth"]);
});

test("a rejection and an exception nobody handled are logged by name and stop the daemon", (t) => {
  const errors = written(t, logger, "error");
  const events = new EventEmitter();
  let stops = 0;
  installProcessHandlers(events, () => {
    stops += 1;
  });
  const secret = new TypeError(`could not read ${"xox" + "b-secret"}`);
  events.emit("unhandledRejection", secret);
  events.emit("unhandledRejection", "a thrown string");
  events.emit("uncaughtException", secret);
  assert.deepEqual(errors(), [
    "unhandled rejection: TypeError",
    "unhandled rejection: string",
    "uncaught exception: TypeError",
  ]);
  assert.equal(stops, 3);
});

// S1: what the daemon does with what the process cannot go on from. Python logged it and went
// on, as asyncio did with a task's exception; here an uncaught exception leaves the process in a
// state Node documents as unsafe, and a Socket Mode connection that is gone for good shows only
// as an unhandled rejection (`test/chat/slack/app/socket-mode.test.ts`): deaf, with the owner
// away, and launchd satisfied. Both take the path of a SIGINT and exit 1, so launchd starts a
// clean daemon and the crash repair runs.

for (const [event, name] of [
  ["uncaughtException", "uncaught exception"],
  ["unhandledRejection", "unhandled rejection"],
] as const) {
  test(`an ${name} stops the daemon as a SIGINT does and exits with 1`, async (t) => {
    const infos = written(t, logger, "info");
    const d = new Daemon(t);
    const { exits, timers, running, events } = mainOf(d);
    await d.connected.wait();
    events.emit(event, new TypeError("boom"));
    await running;
    // No drain, the connection closed, and the lock released.
    assert.deepEqual(
      infos().filter((line) => line.startsWith("stopping") || line === "shutting down"),
      ["shutting down"],
    );
    assert.deepEqual(d.events, ["connect", "app.stop"]);
    await (await singleInstance(d.dir)).release();
    assert.equal(d.signals.listenerCount("SIGTERM"), 0);
    // Exit 1 at once: a timer that does not keep the process alive could not give the code.
    assert.deepEqual(exits, [1]);
    // The only timer is the deadline of the stop, which the stop beat.
    assert.deepEqual(
      timers.map((timer) => timer.seconds),
      [FAILURE_STOP_SECONDS],
    );
  });
}

test("a stop after a failure that does not finish ends the process with 1", async (t) => {
  const errors = written(t, logger, "error");
  const d = new Daemon(t);
  const hung = new AsyncEvent();
  t.mock.method(d.receiver, "stop", async () => {
    await hung.wait(); // a connection that does not close
  });
  const { exits, timers, running, events } = mainOf(d);
  await d.connected.wait();
  events.emit("uncaughtException", new TypeError("boom"));
  await tick();
  // The stop is stuck: the process is not ended yet, and the deadline is armed.
  assert.deepEqual(exits, []);
  assert.deepEqual(
    timers.map((timer) => timer.seconds),
    [FAILURE_STOP_SECONDS],
  );
  timers[0]?.action();
  assert.deepEqual(exits, [1]);
  assert.ok(errors().some((line) => line.includes("did not finish")));
  hung.set(); // let the test's own run end
  await running;
});

test("a failure during the drain ends it as a second signal does", async () => {
  const source = new EventEmitter();
  const stop = new StopSignals(source as SignalSource);
  source.emit("SIGTERM");
  await stop.requested;
  let cut: AbortSignal | null = null;
  const draining = stop.drain(
    (signal) =>
      new Promise<void>((resolve) => {
        cut = signal;
        signal.addEventListener("abort", () => resolve());
      }),
    new FakeClock(),
    DRAIN_LIMIT_SECONDS,
  );
  await tick();
  assert.equal((cut as AbortSignal | null)?.aborted, false);
  stop.fail();
  await draining;
  assert.equal((cut as AbortSignal | null)?.aborted, true);
  assert.equal(stop.failed, true);
  stop.dispose();
});

test("a failure with no stop under way asks for one", async () => {
  const source = new EventEmitter();
  const stop = new StopSignals(source as SignalSource);
  stop.fail();
  await stop.requested;
  assert.equal(stop.failed, true);
  // It is a SIGINT's stop: no drain.
  assert.deepEqual(stop.received, ["SIGINT"]);
  stop.dispose();
});

test("a failure that came before the signals were installed stops the daemon once it is up", async (t) => {
  const d = new Daemon(t);
  const crash = new AbortController();
  crash.abort();
  const code = await d.start({ failure: crash.signal });
  assert.equal(code, 1);
  assert.deepEqual(d.events, ["connect", "app.stop"]);
});

test("run answers 0 for a stop by a signal", async (t) => {
  const d = new Daemon(t);
  d.start();
  await d.booted();
  assert.equal(await d.stop("SIGTERM"), 0);
});

test("an exception after the daemon stopped exits at once", async (t) => {
  const d = new Daemon(t);
  const { exits, timers, running, events } = mainOf(d);
  await d.connected.wait();
  d.signals.emit("SIGTERM");
  await running;
  assert.deepEqual(timers.length, 1);
  events.emit("uncaughtException", new TypeError("late"));
  assert.deepEqual(exits, [1]);
});

test("a second signal ends the drain", async () => {
  const source = new EventEmitter();
  const stop = new StopSignals(source as SignalSource);
  const clock = new FakeClock();
  source.emit("SIGTERM");
  await stop.requested;
  let cut: AbortSignal | null = null;
  const draining = stop.drain(
    (signal) =>
      new Promise<void>((resolve) => {
        cut = signal;
        signal.addEventListener("abort", () => resolve());
      }),
    clock,
    DRAIN_LIMIT_SECONDS,
  );
  await tick();
  assert.equal((cut as AbortSignal | null)?.aborted, false);
  source.emit("SIGINT");
  await draining;
  assert.equal((cut as AbortSignal | null)?.aborted, true);
  assert.deepEqual(stop.received, ["SIGTERM", "SIGINT"]);
  stop.dispose();
  assert.equal(source.listenerCount("SIGTERM"), 0);
});

test("the drain ends at its limit", async () => {
  const source = new EventEmitter();
  const stop = new StopSignals(source as SignalSource);
  const clock = new FakeClock();
  let ended = false;
  const draining = stop.drain(
    (signal) =>
      new Promise<void>((resolve) =>
        signal.addEventListener("abort", () => {
          ended = true;
          resolve();
        }),
      ),
    clock,
    DRAIN_LIMIT_SECONDS,
  );
  await clock.advance(DRAIN_LIMIT_SECONDS - 1);
  assert.equal(ended, false);
  await clock.advance(1);
  await draining;
  assert.equal(ended, true);
  stop.dispose();
});

test("a drain that ends first leaves no wait behind", async () => {
  const source = new EventEmitter();
  const stop = new StopSignals(source as SignalSource);
  const waits: AbortSignal[] = [];
  const clock: Clock = {
    // A wait that only its signal ends, as a timer that has not run out.
    sleep: (_seconds, signal) =>
      new Promise((_, reject) => {
        waits.push(signal as AbortSignal);
        signal?.addEventListener("abort", () => reject(signal.reason));
      }),
    time: () => 0,
  };
  await stop.drain(async () => {}, clock, DRAIN_LIMIT_SECONDS);
  assert.equal(waits.length, 1);
  assert.equal(waits[0]?.aborted, true);
  stop.dispose();
});

test("a signal before the drain starts is counted and does not cut it", async () => {
  // Python cleared the stop event: a second signal during a long repair was lost.
  const source = new EventEmitter();
  const stop = new StopSignals(source as SignalSource);
  source.emit("SIGTERM");
  source.emit("SIGTERM");
  let cut = false;
  await stop.drain(
    async (signal) => {
      cut = signal.aborted;
    },
    new FakeClock(),
    DRAIN_LIMIT_SECONDS,
  );
  assert.equal(cut, false);
  assert.deepEqual(stop.received, ["SIGTERM", "SIGTERM"]);
  stop.dispose();
});

// F1: a listener still in flight when the stop ends. The owner's message is held on the channel
// check (`conversations.members`), the stop arrives, `run` returns, and the check is let go: the
// listener must not start a session, write `state.json` or call Slack. With the real back end a
// session started then keeps the Node process alive, and launchd, which sees a live job, starts
// nothing.

const IN_THREAD = "1780000000.000001";

/** The channel bound, and a thread in it on a session the agent still lists (the prune keeps it). */
function storedThread(d: Daemon): void {
  const state = new StateStore(d.path);
  state.bind(CHANNEL, d.dir);
  state.openThread(CHANNEL, IN_THREAD, "some-id");
  d.backend.listed = [
    { id: "some-id", title: "t", customTitle: null, branch: null, size: null, lastModified: 1 },
  ];
}

for (const [text, signal] of [
  ["!help", "SIGTERM"],
  ["!bypass on", "SIGTERM"],
  ["!status", "SIGTERM"],
  ["hello", "SIGINT"],
] as const) {
  test(`a listener still on the channel check does nothing once the daemon stopped [${text}]`, async (t) => {
    const d = new Daemon(t);
    storedThread(d);
    d.start();
    await d.booted();
    const gate = new AsyncEvent();
    d.slack.gate = gate;
    d.slack.gateMethod = "conversations.members";
    const delivered = d.deliver(reply(text, IN_THREAD));
    await d.slack.gated.wait();
    assert.equal(await d.stop(signal), 0);
    const file = readFileSync(d.path, "utf8");
    const calls = [d.slack, d.replies, d.owner].map((slack) => slack.apiCalls.length);
    gate.set();
    await delivered;
    await tick();
    assert.deepEqual(d.backend.sessions, [], "an agent session was started after the stop");
    assert.equal(readFileSync(d.path, "utf8"), file, "state.json was written after the stop");
    // The held `conversations.members` is recorded as it is let go; nothing follows it.
    assert.deepEqual(
      d.slack.apiCalls.slice(calls[0]).map((call) => call.method),
      ["conversations.members"],
    );
    assert.equal(d.replies.apiCalls.length, calls[1]);
    assert.equal(d.owner.apiCalls.length, calls[2]);
  });
}

// F3: the clients the daemon builds, each in the place it is meant for. One fake for each
// client, so a wiring that hands a part the wrong one is a failing test.

test("a reply is written through the replies client and nothing else is", async (t) => {
  const d = new Daemon(t, { scripts: [{ turns: [sdkMessages("tools")] }] });
  storedThread(d);
  d.start();
  await d.booted();
  await d.deliver(reply("hello", IN_THREAD));
  await d.until(() => d.replies.callsTo("chat.stopStream").length > 0);
  // The reply's own calls: the four that create a message or grow it, and the sink's rewrites.
  const ownCalls = [
    "chat.startStream",
    "chat.appendStream",
    "chat.stopStream",
    "chat.postMessage",
    "chat.update",
  ];
  const methods = new Set(d.replies.apiCalls.map((call) => call.method));
  for (const method of ["chat.startStream", "chat.stopStream"]) {
    assert.ok(methods.has(method), `the replies client never got ${method}`);
  }
  assert.deepEqual(
    [...methods].filter((method) => !ownCalls.includes(method)),
    [],
    "the replies client carried a call that is not the reply's",
  );
  // The shared client carries no part of the reply: a stream, or a post that closes it.
  const shared = d.slack.apiCalls.map((call) => call.method);
  for (const method of ["chat.startStream", "chat.appendStream", "chat.stopStream"]) {
    assert.ok(!shared.includes(method), `${method} went through the shared client`);
  }
  assert.equal(d.owner.apiCalls.length, 0);
  await d.stop();
});

test("each token reaches only the constructor meant for it", async (t) => {
  const d = new Daemon(t, { userToken: true });
  d.owner.responses["auth.test"] = { ok: true, team_id: TEAM, user_id: OWNER };
  d.start();
  await d.booted();
  assert.deepEqual(
    [...d.tokens].sort(([a], [b]) => a.localeCompare(b)),
    [
      ["bot", BOT_TOKEN],
      ["owner", USER_TOKEN],
      ["receiver", APP_TOKEN],
    ],
  );
  // The user token's `auth.test` went to the owner's client, the bot's to the shared one.
  assert.deepEqual(
    d.owner.apiCalls.map((call) => call.method),
    ["auth.test"],
  );
  assert.ok(d.slack.callsTo("auth.test").length === 1);
  assert.equal(d.replies.callsTo("auth.test").length, 0);
  await d.stop();
});

test("the deleter is given the bot client, then the owner's", async (t) => {
  const d = new Daemon(t, { userToken: true });
  d.owner.responses["auth.test"] = { ok: true, team_id: TEAM, user_id: OWNER };
  storedThread(d);
  const own = "1780000000.000002";
  const bots = "1780000000.000003";
  d.slack.responses["conversations.replies"] = {
    ok: true,
    messages: [
      { type: "message", ts: IN_THREAD, user: OWNER, thread_ts: IN_THREAD, text: "x" },
      { type: "message", ts: own, user: OWNER, thread_ts: IN_THREAD, text: "x" },
      { type: "message", ts: bots, user: BOT, bot_id: "B000BOT", thread_ts: IN_THREAD, text: "x" },
    ],
    has_more: false,
    response_metadata: { next_cursor: "" },
  };
  d.start();
  await d.booted();
  // The Home tab's delete is the one path to the deleter: edit mode, then Delete.
  await d.deliver(homeAction({ type: "button", action_id: EDIT_ACTION, value: EDIT_ON }));
  const thread = `${CHANNEL}:${IN_THREAD}`;
  await d.deliver(homeAction({ type: "button", action_id: DELETE_ACTION, value: thread }));
  await d.until(() => d.owner.callsTo("chat.delete").length > 0);
  // What the bot wrote goes with the bot's token, everything else with the owner's.
  assert.deepEqual(
    d.slack.callsTo("chat.delete").map((call) => call.ts),
    [bots],
  );
  assert.deepEqual(
    d.owner.callsTo("chat.delete").map((call) => call.ts),
    [own, IN_THREAD],
  );
  assert.equal(d.replies.callsTo("chat.delete").length, 0);
  await d.stop();
});

test("run as the entry point reports a missing configuration and exits 1", (t) => {
  const home = mkdtempSync(join(tmpdir(), "awaydesk-home-"));
  t.after(() => rmSync(home, { recursive: true, force: true }));
  mkdirSync(join(home, ".config"), { recursive: true });
  const result = spawnSync(process.execPath, [join(ROOT, "src", "main.ts")], {
    env: { HOME: home, PATH: process.env.PATH ?? "" },
    encoding: "utf8",
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /ERROR awaydesk\.main: .*\.env does not exist; see docs\/setup\.md/);
  assert.equal(result.stderr.trim().split("\n").length, 1);
});
