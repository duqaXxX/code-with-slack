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
 *   a clean stop it clears that timer and everything else `run` made is closed (the cleanup's
 *   sleep and the drain's limit are aborted, never left to run out), so the process ends by
 *   itself: nothing here calls `process.exit` on success.
 * - Errors nobody handles. asyncio logged a task's exception that was never retrieved, and a
 *   callback's exception, and went on. A rejected promise or a thrown exception in Node would
 *   end the process at once, without the shutdown below, with open replies and a Claude Code
 *   process per session. `installProcessHandlers` logs each by its name and goes on, as asyncio
 *   did. The message and the stack are never logged: they can quote a request.
 * - The entry point. `import.meta.main` (Node 22.18, `@since v22.18.0` in `@types/node`, and
 *   run on 22.23 and 26.5) is true only for the module the process started with, a symlink in
 *   `node_modules/.bin` included: Node runs the real path. Comparing `process.argv[1]` with
 *   this file's path would fail through that symlink.
 */
import { homedir } from "node:os";
import { join } from "node:path";
import { ClaudeBackend } from "./agent/claude/backend.ts";
import { UsageProbe } from "./agent/claude/usage-probe.ts";
import type { AgentBackend } from "./agent/seam.ts";
import { ChatError } from "./chat/seam.ts";
import { type BuildAppOptions, buildApp, socketReceiver } from "./chat/slack/app/app.ts";
import { ChannelGuard, type Identity } from "./chat/slack/app/guards.ts";
import { prepareUploads, uploadsDir } from "./chat/slack/attachments.ts";
import { ThreadDeleter } from "./chat/slack/delete.ts";
import { Home } from "./chat/slack/home.ts";
import { repairCrash } from "./chat/slack/repair.ts";
import { contextBlock, noticeText } from "./chat/slack/reply/blocks.ts";
import { type ClientOptions, repliesClient, sharedClient } from "./chat/slack/reply/clients.ts";
import { describe } from "./chat/slack/reply/errors.ts";
import { UpdateLimiter } from "./chat/slack/reply/limiter.ts";
import { SlackChat } from "./chat/slack/thread.ts";
import { type Clock, monotonicClock, systemClock } from "./clock.ts";
import { type ChannelLookup, CLEAN_EVERY_SECONDS, clean } from "./core/cleanup.ts";
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

const CHANNEL_NOT_FOUND = "channel_not_found";

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
 * What the cleanup asks about a bound channel: `conversations.info`. Only `channel_not_found` is
 * an answer that the channel is gone; any other failure crosses as a `ChatError` named by
 * Slack's code, which the cleanup logs as no answer.
 */
export function channelLookup(slack: SlackClient): ChannelLookup {
  return async (channelId) => {
    try {
      await slack.conversations.info({ channel: channelId });
      return "there";
    } catch (error) {
      const code = describe(error);
      if (code === CHANNEL_NOT_FOUND) return "gone";
      throw new ChatError(code, { cause: error });
    }
  };
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

/** Start the daemon and return once it has stopped. */
export async function run(options: RunOptions = {}): Promise<void> {
  const configDir = options.configDir ?? CONFIG_DIR;
  const clients = options.clients ?? CLIENTS;
  // Wall time for what is dated (a reply's footer, the page's hours), the monotonic clock for
  // what is paced.
  const wall = options.clock ?? systemClock;
  const steady = options.clock ?? monotonicClock;

  const config = loadConfig(configDir);
  const lock = await singleInstance(configDir);
  let stop: StopSignals | null = null;
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
    const backend = options.backend ?? new ClaudeBackend();
    const probe = new UsageProbe(homedir(), (start, requests) => backend.start(start, requests), {
      clock: options.clock,
    });
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
      await step("close the usage probe", () => probe.close());
      clearInterval(keepAlive);
    }
  } finally {
    try {
      await lock.release();
    } finally {
      stop?.dispose();
    }
  }
}

/**
 * What the process does with a rejection or an exception nobody handled: the name goes to the
 * log and the daemon goes on (see the header).
 */
export function installProcessHandlers(events: ProcessEvents = process): void {
  events.on("unhandledRejection", (reason) => {
    logger.error(`unhandled rejection: ${describe(reason)}`);
  });
  events.on("uncaughtException", (error) => {
    logger.error(`uncaught exception: ${describe(error)}`);
  });
}

/** The entry point: runs the daemon, and ends the process with 1 when it cannot start or fails. */
export async function main(options: MainOptions = {}): Promise<void> {
  const exit = options.exit ?? ((code: number) => process.exit(code));
  installProcessHandlers(options.process);
  try {
    await run(options);
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
