/**
 * The Slack side: every inbound path, each checked on its own before it reaches a session. Port
 * of `build_app` in `slack_app.py`: the Bolt app, and one listener per inbound path (a message,
 * the `file_change` of a clip, a button, a select, a modal, a Home control). The app registers
 * no slash command.
 *
 * What was read in `@slack/bolt` 5.1.0 (`node_modules/@slack/bolt/dist/`), and what follows:
 *
 * - `App.js`, the constructor. With `authorize` and no `token`, `app.client` is built with no
 *   token, and `processEvent` makes a client of its own only for a context that carries a
 *   `botToken` or a `userToken` (`selectToken`). `authorize` below returns the workspace and the
 *   bot user `auth.test` gave at startup and no token: no request costs an API call, and Bolt
 *   never builds a client that could call Slack. Every listener calls Slack through the client
 *   `buildApp` was given.
 * - `App.js`, `initReceiver`. `socketMode: true` with an `appToken` builds a
 *   `SocketModeReceiver` inside the constructor; a `receiver` passed in is used as it is. The
 *   receiver is passed in: the daemon gives `socketReceiver()`, a test one that opens no socket
 *   and feeds `App.processEvent({ body, ack })`, the entry point `SocketModeReceiver.js` itself
 *   calls for each `slack_event`.
 * - `App.js`, `processEvent`. `body` is the envelope as it came, so `team_id` and
 *   `is_ext_shared_channel` of an event's envelope reach the listener; `payload` is the event,
 *   the first action or the view. An event is acknowledged by Bolt before any listener runs
 *   (`await ack()`); an action or a view submission is acknowledged by its listener, and what a
 *   view submission answers (`response_action`) is the argument of `ack`. Every listener whose
 *   constraints match runs, and one that throws reaches `handleError`, the handler set with
 *   `app.error`: an `UnknownError` holding the thrown value in `original`, unless it has a
 *   `code` of its own.
 * - `middleware/builtin.js`. `ignoreSelf` (on by default) drops a `bot_message` of this app and
 *   any event whose `user` is the bot. It is switched off, which differs from `slack_bolt`'s
 *   default (it ignores its own events). That is harmless: the listeners drop what a bot wrote
 *   themselves (`guards.isPromptMessage` refuses any event with a `bot_id`, and the owner check
 *   refuses a user who is not the owner), so the middleware would add nothing. `app.event("message")`
 *   filters nothing else (`onlyEvents`, `matchEventType`): a `message_changed` reaches the
 *   listener, which drops it. `app.action` matches on `action_id` alone, `app.view` on
 *   `callback_id` and the `view_submission` type.
 * - `conversation-store.js`. The default store adds a lookup per event for a state this app
 *   never keeps: switched off (`convoStore: false`).
 */
import { App, LogLevel, type Receiver, SocketModeReceiver } from "@slack/bolt";
import type { WebClient, WebClientOptions } from "@slack/web-api";
import { type Clock, monotonicClock } from "../../../clock.ts";
import type { Config } from "../../../core/config.ts";
import type { Holds } from "../../../core/hold.ts";
import type { Approvals } from "../../../core/requests.ts";
import type { SessionManager } from "../../../core/sessions/manager.ts";
import type { StateStore } from "../../../core/state.ts";
import { download } from "../attachments.ts";
import { BIND_ACTION } from "../bind.ts";
import { HOLD_CANCEL, HOLD_CONTINUE } from "../hold.ts";
import {
  CLEAN_ACTION,
  DELETE_ACTION,
  EDIT_ACTION,
  FILTER_ACTIONS,
  type Home,
  NEW_THREAD_ACTION,
  SHOW_ALL_ACTION,
} from "../home.ts";
import { Listings } from "../openfile/listing.ts";
import { OPEN_BUTTON_ACTION, OPEN_FORM, QUERY_ACTION } from "../openfile/modal.ts";
import { levelLogger } from "../quiet-logger.ts";
import { REQUEST_TIMEOUT_MS } from "../reply/clients.ts";
import type { Limiter } from "../reply/limiter.ts";
import {
  APPROVAL_ALLOW,
  APPROVAL_DENY,
  QUESTION_FORM,
  QUESTION_OPEN,
  QUESTION_SKIP,
} from "../requests.ts";
import { RESUME_ACTION } from "../resume.ts";
import { SETUP_BYPASS, SETUP_EFFORT, SETUP_MODEL, SETUP_START } from "../setup.ts";
import * as voice from "../voice.ts";
import { Answers, type AppParts, errorName, type Fetch, logger } from "./answers.ts";
import { Clips } from "./clip.ts";
import { type ChannelGuard, type Identity, isPromptMessage, messageActor } from "./guards.ts";
import { HomeControls } from "./home-controls.ts";
import { filesOf, Messages } from "./messages.ts";
import { Open } from "./open.ts";
import { Requests } from "./requests.ts";
import { ResumeBind } from "./resume-bind.ts";
import { type Ack, type Payload, str, text } from "./wire.ts";

export type { Fetch, Report } from "./answers.ts";
export { logger, RESTART_WAIT_ROWS } from "./answers.ts";
export { ago, isClear, slackUnescape } from "./messages.ts";
export { boundText } from "./resume-bind.ts";
export { actionKey, clickThread } from "./wire.ts";

export const DECISION_ACTIONS = [APPROVAL_ALLOW, APPROVAL_DENY, QUESTION_SKIP] as const;

/** What `buildApp` is given: every client, store and clock comes from outside. */
export interface BuildAppOptions {
  /** The shared client every handler calls Slack with. */
  readonly slack: WebClient;
  readonly config: Config;
  readonly identity: Identity;
  readonly sessions: SessionManager;
  readonly approvals: Approvals;
  readonly holds: Holds;
  readonly guard: ChannelGuard;
  readonly state: StateStore;
  /** The folder a message's files that are no image are saved in (`attachments.uploadsDir`). */
  readonly uploads: string;
  readonly home: Home;
  /** The `chat.update` budget every reply draws from: `SlackChat.limiter`. */
  readonly limiter: Limiter;
  /** Where Slack's payloads come from: `socketReceiver(config.appToken)` in the daemon. */
  readonly receiver: Receiver;
  /** How a file is downloaded; `attachments.download` with the bot token when absent. */
  readonly fetch?: Fetch;
  /** Every wait of a handler; monotonic seconds when absent. */
  readonly clock?: Clock;
  /** Wall-clock seconds; `Date.now` when absent. */
  readonly now?: () => number;
  /** `!open`'s listings; made over `sessions.repository` when absent. */
  readonly listings?: Listings;
}

/** One thing the app registered a listener for. */
export interface Registration {
  readonly kind: "event" | "action" | "view";
  /** The event type, the `action_id` or the `callback_id`. */
  readonly id: string;
  /** The listener only acknowledges: it reads nothing and does nothing, whoever sent the payload. */
  readonly acknowledgeOnly: boolean;
}

/** The Bolt app with its listeners, and what a stop must end besides the connection. */
export interface BuiltApp {
  readonly app: App;
  /**
   * Every listener `buildApp` registered, as it registered them: what a test compares with its
   * table of owner checks, so that a listener added without a row fails there.
   */
  readonly registered: readonly Registration[];
  /**
   * Tells every listener the daemon is stopping, and gives up the clips still waiting for a
   * transcript, whose timers would keep the process alive. A listener still in flight does
   * nothing from its next step on: it posts, opens a modal, submits and writes nothing.
   */
  close(): void;
}

// The longest wait between two tries to get the connection back.
export const RECONNECT_WAIT_SECONDS = 60;

// The web client `@slack/socket-mode` 3.1.0 asks Slack for a WebSocket URL with. Left to itself
// the library gives it 100 retries, each wait 1.3 times the one before with no upper limit: with
// the network gone for two hours it tried 30 times, the last wait 26 minutes, and was still
// waiting 10 minutes after the network was back (run on mocked timers, 2026-10-10). The waits
// start as the library's and stop growing at the limit, and the client never gives up, so the
// connection is back within the limit of the network's return. A try that gets no answer ends
// at the timeout of the daemon's other clients.
const RECONNECT: WebClientOptions = {
  timeout: REQUEST_TIMEOUT_MS,
  retryConfig: {
    forever: true,
    retries: 100,
    factor: 1.3,
    maxTimeout: RECONNECT_WAIT_SECONDS * 1000,
  },
};

/** What `socketReceiver` can be given besides the token: the network and the pace of a test. */
export interface SocketOptions {
  /** The options of the client that asks Slack for the WebSocket URL (`fetch`, in a test). */
  readonly clientOptions?: WebClientOptions;
  /** Milliseconds the client waits for a ping, and before a reconnect (5000 by default). */
  readonly clientPingTimeout?: number;
}

/**
 * The Socket Mode connection the daemon's app listens on; `app.start()` opens it, `app.stop()`
 * closes it. The libraries' own text is never logged (`levelLogger`): the connection's states
 * are, by the names the client gives them.
 */
export function socketReceiver(appToken: string, options: SocketOptions = {}): SocketModeReceiver {
  const receiver = new SocketModeReceiver({
    appToken,
    logger: levelLogger("socket mode"),
    logLevel: LogLevel.WARN,
    ...(options.clientPingTimeout !== undefined && {
      clientPingTimeout: options.clientPingTimeout,
    }),
    installerOptions: { clientOptions: { ...RECONNECT, ...options.clientOptions } },
  });
  receiver.client.on("connected", () => logger.info("socket mode: connected"));
  receiver.client.on("reconnecting", () => logger.warning("socket mode: reconnecting"));
  receiver.client.on("disconnected", () => logger.info("socket mode: disconnected"));
  return receiver;
}

/** The error a listener threw, under Bolt's wrapping of one that has no `code`. */
function thrown(error: unknown): unknown {
  return typeof error === "object" && error !== null && "original" in error
    ? error.original
    : error;
}

/** The Bolt app, with one listener per inbound path. */
export function buildApp(options: BuildAppOptions): BuiltApp {
  const { config, identity, sessions } = options;
  const stopping = new AbortController();
  const parts: AppParts = {
    slack: options.slack,
    config,
    identity,
    sessions,
    approvals: options.approvals,
    holds: options.holds,
    guard: options.guard,
    state: options.state,
    uploads: options.uploads,
    home: options.home,
    limiter: options.limiter,
    fetch:
      options.fetch ??
      (({ url, mimetype, limit }) => download(url, config.botToken, mimetype, limit)),
    clock: options.clock ?? monotonicClock,
    stopped: stopping.signal,
    now: options.now ?? (() => Date.now() / 1000),
    listings:
      options.listings ??
      new Listings((directory, sessionFolder) => sessions.repository(directory, sessionFolder)),
  };
  const answers = new Answers(parts);
  const requests = new Requests(parts, answers);
  const open = new Open(parts, answers);
  const resumeBind = new ResumeBind(parts, answers);
  const messages = new Messages(parts, answers, { requests, open, resumeBind });
  const clips = new Clips(parts, answers, messages);
  const home = new HomeControls(parts);

  const app = new App({
    receiver: options.receiver,
    // auth.test already ran at startup; who may act is decided by the guards of each listener.
    authorize: async () => ({ teamId: identity.teamId, botUserId: identity.botUserId }),
    ignoreSelf: false,
    convoStore: false,
    logger: levelLogger("bolt"),
    logLevel: LogLevel.WARN,
  });

  const registered: Registration[] = [];

  app.event("message", async ({ event, body }) => {
    const message = event as unknown as Payload;
    const envelope = body as unknown as Payload;
    if (!isPromptMessage(message)) return;
    const [user, team] = messageActor(message, envelope);
    const channel = text(message.channel);
    const ts = str(message.ts);
    const threadTs = str(message.thread_ts || ts);
    if (!(await answers.admitted(user, team, channel, threadTs)) || channel === null) return;
    // (sync) From here to the turn the message takes among its thread's (`Messages.arrival`).
    const spoken = voice.clip(filesOf(message));
    if (spoken !== null) {
      await answers.replyOnFailure(channel, threadTs, () =>
        clips.takeClip(channel, threadTs, ts, message, spoken),
      );
      return;
    }
    // A failure of the prompt itself (or of the session it starts) lands in that session's
    // thread: it is the turn's one push. A word answers where it was typed instead.
    await answers.replyOnFailure(channel, threadTs, () =>
      messages.handleMessage(channel, threadTs, ts, message),
    );
  });

  app.event("file_change", ({ event }) => clips.onFileChange(event as unknown as Payload));
  registered.push(
    { kind: "event", id: "message", acknowledgeOnly: false },
    { kind: "event", id: "file_change", acknowledgeOnly: false },
  );

  /** A click or a select: the listener acknowledges, then checks, on its own. */
  const action = (
    actionId: string,
    listener: (ack: Ack, body: Payload) => Promise<void>,
    options: { readonly acknowledgeOnly?: boolean } = {},
  ): void => {
    registered.push({
      kind: "action",
      id: actionId,
      acknowledgeOnly: options.acknowledgeOnly ?? false,
    });
    app.action(actionId, ({ ack, body }) => listener(() => ack(), body as unknown as Payload));
  };

  /** A modal's submit: what the listener answers is what the modal does next. */
  const view = (callbackId: string, listener: (ack: Ack, body: Payload) => Promise<void>): void => {
    registered.push({ kind: "view", id: callbackId, acknowledgeOnly: false });
    app.view(callbackId, ({ ack, body }) =>
      // The answer is built to the view submission reference by the listener itself.
      listener((answer) => ack(answer as never), body as unknown as Payload),
    );
  };

  action(SETUP_START, requests.onSetupStart);
  action(SETUP_MODEL, requests.onSetupModel);
  // The controls' own state rides on Start's click: a change of either needs nothing here.
  action(SETUP_EFFORT, requests.onSetupEdit, { acknowledgeOnly: true });
  action(SETUP_BYPASS, requests.onSetupEdit, { acknowledgeOnly: true });
  for (const actionId of [HOLD_CONTINUE, HOLD_CANCEL]) action(actionId, requests.onHoldDecision);
  for (const actionId of DECISION_ACTIONS) action(actionId, requests.onDecision);
  action("answer", requests.onAnswer, { acknowledgeOnly: true });
  action(QUESTION_OPEN, requests.onQuestionOpen);
  view(QUESTION_FORM, requests.onQuestionSubmit);

  action(OPEN_BUTTON_ACTION, open.onOpenChoose);
  action(QUERY_ACTION, open.onOpenQuery);
  view(OPEN_FORM, open.onOpenSubmit);

  action(BIND_ACTION, resumeBind.onBind);
  action(RESUME_ACTION, resumeBind.onResume);

  action(NEW_THREAD_ACTION, home.onHomeLink, { acknowledgeOnly: true });
  for (const actionId of FILTER_ACTIONS) action(actionId, home.onHomeFilter);
  action(SHOW_ALL_ACTION, home.onHomeShowAll);
  action(EDIT_ACTION, home.onHomeEdit);
  action(DELETE_ACTION, home.onHomeDelete);
  action(CLEAN_ACTION, home.onHomeClean);

  app.error(async (error) => {
    logger.error(`handler failed: ${errorName(thrown(error))}`);
  });

  return {
    app,
    registered,
    close: () => {
      stopping.abort();
      clips.close();
    },
  };
}
