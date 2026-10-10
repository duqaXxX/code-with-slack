#!/usr/bin/env node
/**
 * `awaydesk`: start the daemon (normally from the LaunchAgent in docs/setup.md). Port of
 * `__main__.py`, and the one module that may import all three layers.
 *
 * What Node changes about the Python entry point:
 *
 * - Signals. `SIGTERM` and `SIGINT` are caught with `process.on`, from the start of `run` up to
 *   the end of the shutdown, as `loop.add_signal_handler` was. Node's signal watchers do not
 *   keep the event loop alive, and a listener removed at the end puts the default disposition
 *   back.
 * - What keeps the process running. A pending promise holds nothing. While the daemon waits for
 *   a signal, the Socket Mode client and the cleanup's timer are what the libraries and the
 *   clock happen to keep alive, so `run` holds one timer of its own until the shutdown ends. On
 *   a stop it clears that timer and closes everything else it made (the cleanup's sleep and the
 *   drain's limit are aborted, never left to run out).
 * - Errors nobody handles. asyncio logged a task's exception that was never retrieved, and a
 *   callback's exception, and went on. A rejected promise or a thrown exception in Node would
 *   end the process at once, without the shutdown below, with open replies and a Claude Code
 *   process per session. `installProcessHandlers` logs each by its name (the message and the
 *   stack can quote a request) and then stops the daemon as a SIGINT would (the shutdown
 *   without a drain: sessions closed, lock released) and ends the process with status 1, so
 *   that launchd starts a clean daemon and the crash repair runs. That differs from the Python
 *   daemon, which went on, for two reasons. Node documents the process as unsafe to continue
 *   after an uncaught exception. And `@slack/socket-mode` 3.1.0 gives no event for a connection
 *   that cannot come back: a reconnect whose request for a new WebSocket URL fails with a
 *   request error, an HTTP error or an unrecoverable platform code is an unawaited call
 *   (`SocketModeClient.delayReconnectAttempt`, called from the `close` listener), so only its
 *   unhandled rejection shows it, and a daemon that went on would stay up deaf, the lock held
 *   and launchd satisfied, with the owner away. Any unhandled rejection therefore stops the
 *   daemon.
 * - The end of the process. `main` ends it with the code `run` gave as soon as `run` returns,
 *   as the Python interpreter ended when `asyncio.run` did. Node would otherwise stay alive
 *   for as long as anything holds its event loop, and the Socket Mode connection does after
 *   every stop: `SocketModeReceiver.stop` of `@slack/bolt` 5.1.0 does not wait for the client's
 *   `disconnect`, the WebSocket stays open once the close frame is sent, and
 *   `@slack/socket-mode` 3.1.0 ends it only when its own ping goes unanswered (measured on
 *   2026-10-10: 5.9 seconds after the stop, three times out of three). In that time the lock is
 *   already free, and launchd, which sees a live job, starts nothing. A stop that a failure
 *   asked for and that has not returned after `FAILURE_STOP_SECONDS` ends the process with 1 on
 *   a timer that does not keep it alive (`deferred`): a shutdown stuck on a connection would
 *   leave the daemon deaf.
 * - The entry point. `import.meta.main` (Node 22.18, `@since v22.18.0` in `@types/node`, and
 *   run on 22.23 and 26.5) is true only for the module the process started with, a symlink in
 *   `node_modules/.bin` included: Node runs the real path. Comparing `process.argv[1]` with
 *   this file's path would fail through that symlink.
 */
import { homedir } from "node:os";
import { join } from "node:path";
import { ClaudeBackend } from "./agent/claude/backend.ts";
import { type BareStart, UsageProbe } from "./agent/claude/usage-probe.ts";
import type { AgentBackend } from "./agent/seam.ts";
import { type BuildAppOptions, buildApp, socketReceiver } from "./chat/slack/app/app.ts";
import { ChannelGuard, type Identity } from "./chat/slack/app/guards.ts";
import { prepareUploads, uploadsDir } from "./chat/slack/attachments.ts";
import { channelLookup } from "./chat/slack/channels.ts";
import { ThreadDeleter } from "./chat/slack/delete.ts";
import { Home } from "./chat/slack/home.ts";
import { repairCrash } from "./chat/slack/repair.ts";
import { contextBlock, noticeText } from "./chat/slack/reply/blocks.ts";
import { type ClientOptions, repliesClient, sharedClient } from "./chat/slack/reply/clients.ts";
import { describe } from "./chat/slack/reply/errors.ts";
import { UpdateLimiter } from "./chat/slack/reply/limiter.ts";
import { SlackChat } from "./chat/slack/thread.ts";
import { type Clock, monotonicClock, systemClock } from "./clock.ts";
import { CLEAN_EVERY_SECONDS, clean } from "./core/cleanup.ts";
import { CONFIG_DIR, ConfigError, loadConfig } from "./core/config.ts";
import { UsageCache } from "./core/footer.ts";
import { Holds } from "./core/hold.ts";
import { AlreadyRunning, singleInstance } from "./core/lock.ts";
import { Approvals } from "./core/requests.ts";
import { SessionManager } from "./core/sessions/manager.ts";
import { StateError, StateStore } from "./core/state.ts";
import * as texts from "./core/texts.ts";
import { getLogger } from "./log.ts";

export const logger = getLogger("awaydesk.main");

// How long a stop waits for running turns before it ends them itself. It holds after
// `launchctl kill TERM`, which only sends the signal; a stop launchd makes itself (`bootout`,
// `kickstart -k`) kills the process after ExitTimeOut, which launchd caps at 60 seconds.
export const DRAIN_LIMIT_SECONDS = 1740;

// How long the stop that a failure asked for may take before `main` ends the process with 1: a
// stop that hangs on a connection or a child leaves the daemon deaf, with nobody to Ctrl-C it.
export const FAILURE_STOP_SECONDS = 30;

/** `action` after `seconds`, on a timer that does not keep the process alive. */
export function deferred(seconds: number, action: () => void): NodeJS.Timeout {
  return setTimeout(action, seconds * 1000).unref();
}

/** The client of the Slack packages every call goes through; its type is all this file names. */
export type SlackClient = ReturnType<typeof sharedClient>;

/** The two signals a stop is asked with. */
export type Stop = "SIGTERM" | "SIGINT";

/** Where the signals come from: the process, or an emitter in a test. */
export interface SignalSource {
  on(signal: Stop, listener: () => void): unknown;
  off(signal: Stop, listener: () => void): unknown;
}

/** The two events of the process that `installProcessHandlers` listens to. */
export interface ProcessEvents {
  on(event: "unhandledRejection", listener: (reason: unknown) => void): unknown;
  on(event: "uncaughtException", listener: (error: Error) => void): unknown;
}

/** The Slack clients of the daemon, one constructor for each way it calls. */
export interface Clients {
  /** The shared client and the one replies are written with, on the bot token. */
  bot(token: string): readonly [shared: SlackClient, replies: SlackClient];
  /** The owner's own client, on the user token. */
  owner(token: string): SlackClient;
}

/**
 * The daemon's two Slack clients on one token (the retry policy is in `reply/clients.ts`): the
 * shared one for everything but a reply, and the one replies are written with.
 */
export function makeClients(
  botToken: string,
  options: ClientOptions = {},
): readonly [shared: SlackClient, replies: SlackClient] {
  return [sharedClient(botToken, options), repliesClient(botToken, options)];
}

export const CLIENTS: Clients = {
  bot: (token) => makeClients(token),
  owner: (token) => sharedClient(token),
};

/** Every outside thing `run` uses, with the real one as the default. */
export interface RunOptions {
  /** `~/.config/awaydesk`. */
  readonly configDir?: string;
  readonly clients?: Clients;
  /** Where Slack's payloads come from: `socketReceiver`. */
  readonly receiver?: (appToken: string) => BuildAppOptions["receiver"];
  /** The agent: Claude Code over the Agent SDK. */
  readonly backend?: AgentBackend;
  /**
   * How the usage probe starts its session: `ClaudeBackend.startBare`. A test that gives
   * `backend` gives this too, or the probe would start the real Claude Code.
   */
  readonly bareStart?: BareStart;
  /**
   * Aborts when the process met what it cannot go on from (see the header): the daemon stops as
   * a SIGINT does, and `run` answers 1.
   */
  readonly failure?: AbortSignal;
  /** Every wait of the daemon; the system clock for wall time and the monotonic one for pauses. */
  readonly clock?: Clock;
  /** `process`. */
  readonly signals?: SignalSource;
  /** `new StateStore(path)`. */
  readonly openState?: (path: string) => StateStore;
  /** The folder of the files saved from messages: `$TMPDIR/awaydesk`. */
  readonly uploads?: string;
}

export interface MainOptions extends RunOptions {
  /** `process.exit`. */
  readonly exit?: (code: number) => void;
  /** `process`. */
  readonly process?: ProcessEvents;
  /** Runs an action after the seconds given, on a timer that does not keep the process alive. */
  readonly later?: (seconds: number, action: () => void) => unknown;
}

/** The text of a field `auth.test` must answer, which a log line and an identity rely on. */
function authField(
  answer: { readonly user_id?: string | undefined; readonly team_id?: string | undefined },
  key: "user_id" | "team_id",
): string {
  const value = answer[key];
  if (typeof value !== "string" || value === "") throw new Error(`auth.test answered no ${key}`);
  return value;
}

/**
 * The v1-to-v2 migration notice, once per channel, top-level: not a reply to any message, so it
 * carries no thread_ts. A channel whose post failed keeps its flag for the next start.
 */
export async function postUpgradeNotices(slack: SlackClient, state: StateStore): Promise<void> {
  for (const channelId of state.pendingNotices()) {
    try {
      await slack.chat.postMessage({
        channel: channelId,
        text: texts.UPGRADE_NOTICE,
        blocks: [contextBlock(noticeText(texts.UPGRADE_NOTICE))],
        unfurl_links: false,
        unfurl_media: false,
      });
    } catch (error) {
      logger.warning(`could not post the upgrade notice in ${channelId}: ${describe(error)}`);
      continue;
    }
    state.clearNotice(channelId);
  }
}

/**
 * The scheduled cleanup of `state.json`: a pass every `seconds`, none once a stop has begun (the
 * sessions are closing, and the next start cleans anyway). Ends when `signal` aborts; a pass in
 * flight is finished first, since a call to Slack cannot be cut short.
 */
export async function cleanEvery(options: {
  readonly seconds: number;
  readonly clean: () => Promise<void>;
  readonly sessions: { readonly draining: boolean };
  readonly clock: Clock;
  readonly signal: AbortSignal;
}): Promise<void> {
  for (;;) {
    try {
      await options.clock.sleep(options.seconds, options.signal);
    } catch {
      return; // the stop: the only thing that ends a sleep
    }
    if (options.sessions.draining) return;
    await options.clean();
  }
}

/**
 * What deletes a thread for the Home tab, when the owner's user token is configured. The token
 * must be the owner's own, in the bot's workspace: it deletes as whoever it belongs to.
 */
export async function deleter(
  userToken: string | null,
  slack: SlackClient,
  identity: Identity,
  state: StateStore,
  sessions: Pick<SessionManager, "release" | "free">,
  clients: Pick<Clients, "owner"> = CLIENTS,
): Promise<ThreadDeleter | null> {
  if (userToken === null) return null;
  const owner = clients.owner(userToken);
  const who = await owner.auth.test();
  if (
    authField(who, "user_id") !== identity.ownerUserId ||
    authField(who, "team_id") !== identity.teamId
  ) {
    throw new ConfigError("SLACK_USER_TOKEN must be the owner's own token, in the bot's workspace");
  }
  return new ThreadDeleter(slack, owner, {
    botUserId: identity.botUserId,
    ownerUserId: identity.ownerUserId,
    state,
    release: (channelId, threadTs) => sessions.release(channelId, threadTs),
    free: (channelId, threadTs) => sessions.free(channelId, threadTs),
  });
}

/**
 * The signals of a stop. The first one starts it: `SIGTERM` lets running turns finish
 * (`drain`), `SIGINT` does not, since a Ctrl-C in a terminal reaches the Claude Code processes
 * too, which share the daemon's process group. A signal that arrives while the drain runs ends
 * the wait. One that arrives before the drain starts (during a long repair, say) is only
 * counted, as Python's `stop.clear()` did.
 */
export class StopSignals {
  readonly received: Stop[] = [];
  /** Whether the stop was asked for by a failure, not by a signal (`fail`). */
  failed = false;
  /** Settles on the first signal. */
  readonly requested: Promise<void>;
  readonly #source: SignalSource;
  readonly #listeners: Array<readonly [Stop, () => void]> = [];
  #requested: () => void = () => {};
  #draining: AbortController | null = null;

  constructor(source: SignalSource) {
    this.#source = source;
    this.requested = new Promise((resolve) => {
      this.#requested = resolve;
    });
    for (const signal of ["SIGTERM", "SIGINT"] as const) {
      const listener = () => {
        this.received.push(signal);
        this.#requested();
        this.#draining?.abort();
      };
      this.#listeners.push([signal, listener]);
      source.on(signal, listener);
    }
  }

  /**
   * Asks for a stop the process cannot go without (an uncaught exception, a connection that is
   * gone for good): counted as a SIGINT, so no drain starts and one under way is cut short.
   */
  fail(): void {
    this.failed = true;
    this.received.push("SIGINT");
    this.#requested();
    this.#draining?.abort();
  }

  /**
   * Runs `drain` until it ends, `limitSeconds` pass, or another signal arrives, the last two
   * through the signal `drain` is given. Leaves no timer behind.
   */
  async drain(
    drain: (cutShort: AbortSignal) => Promise<void>,
    clock: Clock,
    limitSeconds: number,
  ): Promise<void> {
    const cut = new AbortController();
    this.#draining = cut;
    const limit = clock.sleep(limitSeconds, cut.signal).then(
      () => cut.abort(),
      () => undefined, // ended by the drain
    );
    try {
      await drain(cut.signal);
    } finally {
      cut.abort();
      this.#draining = null;
      await limit;
    }
  }

  /** Stops listening: the default disposition of the signals returns. */
  dispose(): void {
    for (const [signal, listener] of this.#listeners.splice(0)) this.#source.off(signal, listener);
  }
}

/** Runs one step of the shutdown; a step that fails is logged by its name, never skips the rest. */
async function step(name: string, work: () => unknown): Promise<void> {
  try {
    await work();
  } catch (error) {
    logger.error(`shutdown: could not ${name}: ${describe(error)}`);
  }
}

/**
 * Start the daemon and return once it has stopped: 0 for a stop by a signal, 1 for one a failure
 * asked for (`RunOptions.failure`).
 */
export async function run(options: RunOptions = {}): Promise<number> {
  const configDir = options.configDir ?? CONFIG_DIR;
  const clients = options.clients ?? CLIENTS;
  // Wall time for what is dated (a reply's footer, the page's hours), the monotonic clock for
  // what is paced.
  const wall = options.clock ?? systemClock;
  const steady = options.clock ?? monotonicClock;

  const config = loadConfig(configDir);
  const lock = await singleInstance(configDir);
  let stop: StopSignals | null = null;
  const onFailure = (): void => stop?.fail();
  try {
    const state = (options.openState ?? ((path) => new StateStore(path)))(
      join(configDir, "state.json"),
    );
    const uploads = options.uploads ?? uploadsDir();
    await prepareUploads(uploads);
    const [slack, replySlack] = clients.bot(config.botToken);
    const auth = await slack.auth.test();
    const identity: Identity = {
      ownerUserId: config.ownerUserId,
      teamId: authField(auth, "team_id"),
      botUserId: authField(auth, "user_id"),
    };
    const claude = options.backend === undefined ? new ClaudeBackend() : null;
    const backend = options.backend ?? (claude as ClaudeBackend);
    const bare =
      options.bareStart ??
      ((folder, requests) => (claude as ClaudeBackend).startBare(folder, requests));
    const probe = new UsageProbe(homedir(), bare, { clock: options.clock });
    const approvals = new Approvals();
    const holds = new Holds();
    const chat = new SlackChat({
      slack,
      replies: replySlack,
      identity,
      limiter: new UpdateLimiter({ clock: steady }),
      clock: wall,
    });
    const sessions = new SessionManager({
      chat,
      agent: backend,
      state,
      approvals,
      holds,
      usage: new UsageCache(() => probe.read()),
      clock: steady,
    });
    const threadDeleter = await deleter(
      config.userToken,
      slack,
      identity,
      state,
      sessions,
      clients,
    );
    const home = new Home(slack, {
      ownerUserId: identity.ownerUserId,
      teamId: identity.teamId,
      state,
      sessionsOf: (directory) => backend.listSessions(directory),
      ...(threadDeleter !== null && {
        delete: (channelId, threadTs) => threadDeleter.delete(channelId, threadTs),
        clean: (channelId) => threadDeleter.clean(channelId),
      }),
      clock: wall,
    });
    const { app, close } = buildApp({
      slack,
      config,
      identity,
      sessions,
      approvals,
      holds,
      guard: new ChannelGuard(slack, identity),
      state,
      uploads,
      home,
      limiter: chat.limiter,
      receiver: (options.receiver ?? socketReceiver)(config.appToken),
      clock: steady,
    });

    const cleanOnce = (): Promise<void> =>
      clean(channelLookup(slack), state, {
        alive: (directory) => backend.aliveSessions(directory),
        live: () => sessions.liveThreads(),
      });
    const cleaning = new AbortController();
    let cleaned: Promise<void> | null = null;
    // Held until the shutdown is done (see the header).
    const keepAlive = setInterval(() => {}, 2 ** 31 - 1);
    try {
      // Installed before the repair, not after, so a SIGTERM that arrives during a long repair
      // (many threads, each Slack call under the shared rate limiter) is caught and not left to
      // the default disposition, which ends the process at once and skips the shutdown below
      // (the single-instance lock, the Socket Mode connection, every session). The repair still
      // runs to completion either way: the signal is only lost if nothing listens yet.
      const signals = new StopSignals(options.signals ?? process);
      stop = signals;
      // A failure that came earlier is applied now, like a signal that came during the repair.
      if (options.failure?.aborted) signals.fail();
      else options.failure?.addEventListener("abort", onFailure, { once: true });

      // Repaired, then cleaned, before the Socket Mode connection opens: `slack` (already
      // `auth.test`'d above) works without it, and opening Socket Mode is what starts delivering
      // events. A pruned thread's leftovers must be repaired first.
      await repairCrash(slack, state, chat.limiter);
      await cleanOnce();
      cleaned = cleanEvery({
        seconds: CLEAN_EVERY_SECONDS,
        clean: cleanOnce,
        sessions,
        clock: steady,
        signal: cleaning.signal,
      });
      // The session index (the owner's Home tab): written once what the last run left is
      // repaired and pruned, then again whenever state.json's sessions change.
      state.onSessionsChange = (changed) => home.request(changed);
      home.request();

      await app.start();
      logger.info(`connected to Slack workspace ${identity.teamId}`);
      await postUpgradeNotices(slack, state);
      await signals.requested;
      // launchd stops and restarts with SIGTERM: the turns already running finish first. Not on
      // SIGINT: see `StopSignals`. A second signal stops without waiting.
      if (signals.received[0] === "SIGTERM") {
        logger.info("stopping: letting running turns finish");
        await signals.drain((cutShort) => sessions.drain(cutShort), steady, DRAIN_LIMIT_SECONDS);
      }
    } finally {
      logger.info("shutting down");
      cleaning.abort();
      if (cleaned !== null) await step("stop the cleanup", () => cleaned);
      await step("stop the Slack connection", () => app.stop());
      // The timers of clips still waiting for a transcript would keep the process alive.
      await step("end the clips' waits", () => close());
      await step("close the sessions", () => sessions.closeAll());
      // After the sessions: the page shows them as this stop left them.
      await step("close the session index", () => home.close());
      await step("stop the usage probe", () => probe.stop());
      clearInterval(keepAlive);
    }
  } finally {
    options.failure?.removeEventListener("abort", onFailure);
    try {
      await lock.release();
    } finally {
      stop?.dispose();
    }
  }
  return stop?.failed ? 1 : 0;
}

/**
 * What the process does with a rejection or an exception nobody handled: the name goes to the
 * log and `stop` is asked for, which stops the daemon (see the header).
 */
export function installProcessHandlers(events: ProcessEvents, stop: () => void): void {
  events.on("unhandledRejection", (reason) => {
    logger.error(`unhandled rejection: ${describe(reason)}`);
    stop();
  });
  events.on("uncaughtException", (error) => {
    logger.error(`uncaught exception: ${describe(error)}`);
    stop();
  });
}

/** The entry point: runs the daemon, then ends the process: 0 after a stop, 1 after a failure. */
export async function main(options: MainOptions = {}): Promise<void> {
  const exit = options.exit ?? ((code: number) => process.exit(code));
  const later = options.later ?? deferred;
  const failure = new AbortController();
  installProcessHandlers(options.process ?? process, () => {
    if (failure.signal.aborted) return;
    failure.abort();
    later(FAILURE_STOP_SECONDS, () => {
      logger.error("the stop after a failure did not finish: ending the process");
      exit(1);
    });
  });
  try {
    const code = await run({ ...options, failure: failure.signal });
    // At once: the Socket Mode connection outlives the stop (see the header).
    exit(code);
  } catch (error) {
    if (
      error instanceof ConfigError ||
      error instanceof StateError ||
      error instanceof AlreadyRunning
    ) {
      logger.error(error.message);
    } else {
      logger.error(`stopped by an unexpected error: ${describe(error)}`);
    }
    exit(1);
  }
}

if (import.meta.main) void main();
