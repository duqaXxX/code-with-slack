/**
 * The Slack test double: a real `WebClient` with the network replaced, and the fake clock.
 * Port of the Slack half of `tests/fakes.py`.
 *
 * Python's fake subclassed `AsyncWebClient` and overrode `api_call`. Here it subclasses
 * `WebClient` and overrides `apiCall`, which is the same funnel (verified in
 * `@slack/web-api` 8.2.0, `dist/methods.js` and `dist/WebClient.js`): every bound method, such as
 * `client.chat.postMessage`, is `self.apiCall.bind(self, method)`, evaluated in the `Methods`
 * constructor against the instance's prototype chain, so an override on a subclass is the one
 * bound. `chat.startStream`, `chat.appendStream`, `chat.stopStream`, `assistant.threads.setStatus`,
 * `views.publish`, `reactions.add` and `reactions.remove` are all plain `bindApiCall` entries.
 * `files.uploadV2` is the exception: it is bound to `filesUploadV2` directly, which does its work
 * through `files.getUploadURLExternal` and `files.completeUploadExternal` (both `apiCall`) and
 * posts the bytes with `makeRequest`, which ends in the client's `fetch` option. The fake hands
 * the client its own `fetch` for that one step, which is what `_upload_file` was in Python.
 *
 * What `apiCalls` and the golden files hold is the call as it reaches the funnel, normalised:
 * - no `token` (a per-call credential, never part of the logical call);
 * - a field that is `undefined` or `null` is dropped, as the client's own body serialiser drops it;
 * - `blocks`, `chunks`, `attachments`, `view`, `metadata` and `files` are parsed values: a JSON
 *   string given for one of them is parsed (the client JSON-encodes objects itself on the wire);
 * - the whole record is a deep copy, so a later change to what the caller passed does not rewrite
 *   what the call carried.
 */
import { constants } from "node:os";
import { setTimeout as sleep } from "node:timers/promises";
import {
  type FetchFunction,
  type WebAPICallResult,
  WebAPIHTTPError,
  WebAPIPlatformError,
  WebAPIRateLimitedError,
  WebAPIRequestError,
  WebClient,
} from "@slack/web-api";
import { type Json, type JsonObject, slackPayload } from "./fixtures.ts";

export const OWNER = "U000ALICE";
export const STRANGER = "U000BOB";
export const TEAM = "T000TEAM";
export const OTHER_TEAM = "T000OTHER";
export const CHANNEL = "C000CHAN";
export const OTHER_CHANNEL = "C000CHN2";
export const BOT = "U000BOT";
// The root message's ts of a synthetic thread: a Slack thread_ts is that message's own epoch time.
export const THREAD = "1780000000.000001";
export const OTHER_THREAD = "1780000000.000002";

// The answers of the two calls `filesUploadV2` makes, in the shape their reference examples give
// (docs.slack.dev/reference/methods/files.getUploadURLExternal and files.completeUploadExternal,
// read 2026-10-05). The third step posts the bytes to the URL the first returns.
export const UPLOAD_URL: JsonObject = {
  ok: true,
  upload_url: "https://files.slack.com/upload/v1/ABC123...",
  file_id: "F000FILE",
};
export const UPLOAD_DONE: JsonObject = { ok: true, files: [{ id: "F000FILE", title: "title" }] };

/** The arguments of a call, normalised (see the top of this file). */
export type Args = JsonObject;

/** One call that reached the funnel, in the order it arrived. */
export interface SlackCall {
  readonly method: string;
  readonly args: Args;
}

/** What a call answers: Slack's answer, or an error the call throws. */
export type Answer = JsonObject | Error;

/**
 * What `FakeSlack.responses` holds for a method: an answer; a function of the call's arguments,
 * which returns the answer (or the error); or a sequence, one answer per call with the last one
 * repeating.
 */
export type Scripted = Answer | Answer[] | ((args: Args) => Answer | Answer[]);

/** The clock a source module takes: the reply sink's debounce and 280-second stream deadline. */
export interface Clock {
  /** Resolves `seconds` from now; rejects with the signal's reason when it aborts first. */
  sleep(seconds: number, signal?: AbortSignal): Promise<void>;
  /** Seconds on this clock's own scale. */
  time(): number;
}

// What a test needs to build the errors the client throws. Each is built the way the client
// builds it (`dist/errors.js`, `dist/WebClient.js` `apiCall` and `makeRequest`), so a provider
// that tells them apart by class or `code` sees what it would see on the wire.

/** Slack answered `{ok: false, error}`: `WebAPIPlatformError`, `code` `slack_webapi_platform_error`. */
export function rejected(error: string, fields: JsonObject = {}): WebAPIPlatformError {
  return new WebAPIPlatformError({ ok: false, error, response_metadata: {}, ...fields });
}

/** `fetch` failed before an answer: the client wraps it in `WebAPIRequestError`. */
function requestFailed(cause: Error): WebAPIRequestError {
  return new WebAPIRequestError(new TypeError("fetch failed", { cause }));
}

function systemError(message: string, name: keyof typeof constants.errno): Error {
  return Object.assign(new Error(message), { code: name, errno: -constants.errno[name] });
}

/** Slack applied the call, then the connection reset (Python: `aiohttp.ClientOSError(104, ...)`). */
export function connectionReset(): WebAPIRequestError {
  return requestFailed(systemError("read ECONNRESET", "ECONNRESET"));
}

/** No route to Slack (Python: `aiohttp.ClientConnectionError("network down")`). */
export function networkDown(): WebAPIRequestError {
  return requestFailed(systemError("connect ENETUNREACH", "ENETUNREACH"));
}

/** The client's own timeout fired: `fetch` rejects with a `TimeoutError` DOMException. */
export function timedOut(): WebAPIRequestError {
  return new WebAPIRequestError(
    new DOMException("The operation was aborted due to timeout", "TimeoutError"),
  );
}

/** HTTP 429 with a `Retry-After`, on a client built with `rejectRateLimitedCalls`. */
export function rateLimited(retryAfter = 1): WebAPIRateLimitedError {
  return new WebAPIRateLimitedError(retryAfter);
}

/** Any HTTP status other than 200 and 429. */
export function httpError(status = 500, statusText = "Internal Server Error"): WebAPIHTTPError {
  return new WebAPIHTTPError(status, statusText, {}, "");
}

/** `asyncio.Event`: `wait` resolves once the event is set, at once when it already is. */
export class AsyncEvent {
  #isSet = false;
  #waiters: Array<() => void> = [];

  isSet(): boolean {
    return this.#isSet;
  }

  set(): void {
    this.#isSet = true;
    for (const wake of this.#waiters.splice(0)) wake();
  }

  clear(): void {
    this.#isSet = false;
  }

  wait(): Promise<void> {
    if (this.#isSet) return Promise.resolve();
    return new Promise((resolve) => this.#waiters.push(resolve));
  }
}

function object(value: Json | undefined, what: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`${what} is not an object`);
  }
  return value;
}

function list(value: Json | undefined, what: string): Json[] {
  if (!Array.isArray(value)) throw new TypeError(`${what} is not a list`);
  return value;
}

/** A list of objects, `[]` for a field that is absent (Python: `list(args.get(key) or [])`). */
function objects(value: Json | undefined, what: string): JsonObject[] {
  return value ? list(value, what).map((item) => object(item, what)) : [];
}

function need(from: JsonObject, key: string): Json {
  const value = from[key];
  if (value === undefined) throw new Error(`no ${key}`);
  return value;
}

/** Python's `str(value)` for the strings and numbers a payload holds. */
function str(value: Json | undefined): string {
  return value === undefined ? "None" : String(value);
}

/** Python's `str.strip()`: its whitespace is not `String.prototype.trim`'s (it adds U+001C to U+001F and U+0085, and has no U+FEFF). */
function strip(text: string): string {
  const space = (char: string | undefined): boolean => {
    if (char === undefined) return false;
    const code = char.charCodeAt(0);
    return (
      (code >= 0x09 && code <= 0x0d) ||
      (code >= 0x1c && code <= 0x20) ||
      code === 0x85 ||
      code === 0xa0 ||
      code === 0x1680 ||
      (code >= 0x2000 && code <= 0x200a) ||
      code === 0x2028 ||
      code === 0x2029 ||
      code === 0x202f ||
      code === 0x205f ||
      code === 0x3000
    );
  };
  let start = 0;
  let end = text.length;
  while (start < end && space(text[start])) start += 1;
  while (end > start && space(text[end - 1])) end -= 1;
  return text.slice(start, end);
}

const PARSED_FIELDS = new Set(["blocks", "chunks", "attachments", "view", "metadata", "files"]);

function toJson(value: unknown, where: string): Json {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") return value;
  if (Array.isArray(value)) return value.map((item, i) => toJson(item, `${where}[${i}]`));
  if (typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    const out: JsonObject = {};
    for (const [key, item] of Object.entries(value)) {
      if (item !== undefined && item !== null) out[key] = toJson(item, `${where}.${key}`);
    }
    return out;
  }
  throw new TypeError(`${where} is not JSON: ${Object.prototype.toString.call(value)}`);
}

/** The arguments as `apiCalls` keeps them (see the top of this file). */
function normalise(options: Record<string, unknown>): Args {
  const args: Args = {};
  for (const [key, value] of Object.entries(options)) {
    if (key === "token" || value === undefined || value === null) continue;
    let parsed = value;
    if (typeof value === "string" && PARSED_FIELDS.has(key)) {
      try {
        parsed = JSON.parse(value);
      } catch {
        parsed = value;
      }
    }
    args[key] = toJson(parsed, key);
  }
  return args;
}

/** One message of the fake workspace, as Slack keeps it: a post, or a stream. A stream takes
 * `chunks` until it is stopped (by `chat.stopStream`, or by Slack after its lifetime:
 * `FakeSlack.expire`); once stopped it takes `chat.update`, which replaces what it shows. */
export class FakeMessage {
  ts: string;
  text = "";
  blocks: JsonObject[] = [];
  streaming = false;
  chunks: JsonObject[] = [];
  /** A chat.update replaced the stream's own content. */
  updated = false;
  deleted = false;

  constructor(ts: string) {
    this.ts = ts;
  }
}

/** A task card as one plain object, whether it came as a `task_update` chunk (strings) or a
 * `task_card` block (rich text, as Slack reads it back). */
export function cardOf(blockOrChunk: JsonObject): JsonObject {
  const plain = (value: Json | undefined): string | null => {
    if (typeof value === "object" && value !== null && !Array.isArray(value)) {
      return list(need(value, "elements"), "elements")
        .map((section) =>
          list(need(object(section, "section"), "elements"), "elements")
            .map((element) => str(object(element, "element").text ?? ""))
            .join(""),
        )
        .join("");
    }
    return value === undefined || value === null ? null : String(value);
  };
  const card: JsonObject = {
    id: "id" in blockOrChunk ? (blockOrChunk.id ?? null) : (blockOrChunk.task_id ?? null),
    title: need(blockOrChunk, "title"),
    status: need(blockOrChunk, "status"),
  };
  for (const key of ["details", "output"]) {
    const value = plain(blockOrChunk[key]);
    if (value !== null) card[key] = value;
  }
  return card;
}

type FetchResponse = Awaited<ReturnType<FetchFunction>>;

/** The step of `filesUploadV2` that posts the bytes: answers 200 and keeps what was posted. */
function uploadFetch(uploaded: FakeUpload[]): FetchFunction {
  return async (url, init) => {
    const form = init?.body;
    const file = form instanceof FormData ? form.get("body") : null;
    if (!(file instanceof Blob)) throw new Error("an upload without a body");
    uploaded.push({ url: String(url), data: Buffer.from(await file.arrayBuffer()) });
    const response: FetchResponse = {
      ok: true,
      status: 200,
      statusText: "OK",
      url: String(url),
      headers: { get: () => null, entries: () => [] },
      arrayBuffer: async () => new ArrayBuffer(0),
      json: async () => ({}),
      text: async () => "OK",
    };
    return response;
  };
}

/** The bytes of a file posted to an upload URL, with that URL. */
export interface FakeUpload {
  readonly url: string;
  readonly data: Buffer;
}

const GATE_DEFAULT = "chat.stopStream";

/**
 * The real `WebClient`, with the network replaced: every method builds its real arguments and
 * ends in `apiCall`, which records them and answers from `responses` or the recordings.
 *
 * Streams behave as the recordings measured (2026-09-28 and 2026-09-29): an open stream refuses
 * `chat.update` (`streaming_state_conflict`) and `chat.delete` (`cant_delete_message`); a stream
 * that is not open refuses `chat.appendStream` and `chat.stopStream`
 * (`message_not_in_streaming_state`).
 */
export class FakeSlack extends WebClient {
  /** Every call that reached the funnel (Python: `calls`; `calls` is the client's own Web API
   * namespace, `client.calls.add`). */
  apiCalls: SlackCall[] = [];
  /** The ts of every chat.postMessage, in order. */
  postedTs: string[] = [];
  /** The ts of every chat.startStream, in order. */
  streamTs: string[] = [];
  /** Both, in the order the messages were created. */
  createdTs: string[] = [];
  messages = new Map<string, FakeMessage>();
  /** Streams that stopped, by the daemon's stop or by `expire`. */
  streamEnds = 0;
  /** Every call waits this many seconds before answering, as a slow Slack API round trip would. */
  delay = 0;
  /**
   * While set, a call to `gateMethod` waits here until the event is set, after `gated` says one
   * arrived: a test holds a write open and acts inside it, with no race against a timer.
   */
  gate: AsyncEvent | null = null;
  gateMethod = GATE_DEFAULT;
  gated = new AsyncEvent();
  /**
   * A response may be an error: the call throws it, as a failed request would. It may be a
   * function of the call's arguments, which returns the answer (or the error).
   */
  responses: Record<string, Scripted> = {
    "auth.test": slackPayload("api-auth-test"),
    "conversations.info": slackPayload("api-conversations-info"),
    "conversations.members": slackPayload("api-conversations-members"),
    "chat.postMessage": slackPayload("api-chat-postMessage"),
    "chat.startStream": slackPayload("api-chat-startStream"),
    "chat.getPermalink": slackPayload("api-chat-getPermalink"),
    "files.getUploadURLExternal": structuredClone(UPLOAD_URL),
    "files.completeUploadExternal": structuredClone(UPLOAD_DONE),
  };
  /** The bytes of every file posted to an upload URL, with that URL. */
  readonly uploaded: FakeUpload[];

  constructor() {
    const uploaded: FakeUpload[] = [];
    // No token, no network: `apiCall` never reaches `makeRequest`, and the upload step reaches
    // only the `fetch` given here. No retry, since a call that fails here fails once.
    super(undefined, { fetch: uploadFetch(uploaded), retryConfig: { retries: 0 } });
    this.uploaded = uploaded;
  }

  #newTs(): string {
    return `1790000000.${String(this.createdTs.length + 1).padStart(6, "0")}`;
  }

  /** Slack closes a stream that lived 5 minutes and never stopped (measured). */
  expire(ts: string): void {
    const message = this.messages.get(ts);
    if (message === undefined) throw new Error(`no message ${ts}`);
    message.streaming = false;
    this.streamEnds += 1;
  }

  /** Slack's answer when the stream's state refuses `method`, or null when it allows it. */
  #streamState(method: string, args: Args): JsonObject | null {
    const message = this.messages.get(String(args.ts));
    if (message === undefined) return null;
    if ((method === "chat.appendStream" || method === "chat.stopStream") && !message.streaming) {
      return { ok: false, error: "message_not_in_streaming_state" };
    }
    if (method === "chat.update" && message.streaming) {
      return { ok: false, error: "streaming_state_conflict" };
    }
    if (method === "chat.delete" && message.streaming) {
      return { ok: false, error: "cant_delete_message" };
    }
    return null;
  }

  /** Keep what each message shows, as the recordings read back. */
  #record(method: string, args: Args, answer: JsonObject): void {
    if (method === "chat.postMessage" || method === "chat.startStream") {
      if (answer.ts === undefined) throw new Error(`${method} answered no ts`);
      const ts = str(answer.ts);
      this.createdTs.push(ts);
      const message = new FakeMessage(ts);
      this.messages.set(ts, message);
      if (method === "chat.postMessage") {
        this.postedTs.push(ts);
        message.text = str(args.text ?? "");
        message.blocks = objects(args.blocks, "blocks");
      } else {
        this.streamTs.push(ts);
        message.streaming = true;
        message.chunks = objects(args.chunks, "chunks");
      }
      return;
    }
    const message = this.messages.get(String(args.ts));
    if (message === undefined) return;
    if (method === "chat.appendStream") {
      message.chunks.push(...objects(args.chunks, "chunks"));
    } else if (method === "chat.stopStream") {
      message.chunks.push(...objects(args.chunks, "chunks"));
      message.streaming = false;
      message.blocks = objects(args.blocks, "blocks");
      this.streamEnds += 1;
    } else if (method === "chat.update") {
      message.updated = true;
      message.text = str(args.text ?? "");
      message.blocks = objects(args.blocks, "blocks");
    } else if (method === "chat.delete") {
      message.deleted = true;
    }
  }

  override async apiCall(
    method: string,
    options: Record<string, unknown> = {},
  ): Promise<WebAPICallResult> {
    // Bound to `filesUploadV2` by the client itself: its inner calls come back through here.
    if (method === "files.uploadV2") return super.apiCall(method, options);
    if (this.delay) await sleep(this.delay * 1000);
    if (this.gate !== null && method === this.gateMethod) {
      this.gated.set();
      await this.gate.wait();
    }
    const args = normalise(options);
    this.apiCalls.push({ method, args });
    const scripted = Object.hasOwn(this.responses, method) ? this.responses[method] : undefined;
    let answer: Answer | Answer[] | undefined;
    if (scripted === undefined) answer = { ok: true };
    else answer = typeof scripted === "function" ? scripted(args) : scripted;
    if (
      (method === "chat.appendStream" || method === "chat.stopStream") &&
      scripted === undefined
    ) {
      // Measured 2026-09-29: both answer the channel and the message's ts.
      answer = { ok: true, channel: args.channel ?? null, ts: args.ts ?? null };
    }
    if (method === "conversations.replies" && scripted === undefined) answer = this.#thread(args);
    if (Array.isArray(answer)) {
      // A scripted sequence: one answer per call, the last repeats.
      answer = answer.length > 1 ? answer.shift() : answer[0];
    }
    if (answer === undefined) throw new Error(`${method} has an empty sequence of answers`);
    if (answer instanceof Error) throw answer;
    if ((method === "chat.postMessage" || method === "chat.startStream") && answer === scripted) {
      answer = { ...answer, ts: this.#newTs() }; // the default: a new ts for each message
    }
    const refused = this.#streamState(method, args);
    if (refused !== null) answer = refused;
    if (answer.ok !== false) this.#record(method, args, answer);
    // As `WebClient.apiCall` builds its result and refuses it.
    const result = structuredClone(answer);
    result.response_metadata ??= {};
    if (!result.ok)
      throw new WebAPIPlatformError(result as unknown as WebAPICallResult & { error: string });
    return result as unknown as WebAPICallResult;
  }

  /**
   * A stream's `text` as Slack reads it back (recorded 2026-09-28, `message-mixed-*` and
   * `message-markdown-*`): the blank lines kept, `**b**` as `*b*`, a heading without its `## `, a
   * list bullet as `•`, a link as `<url|label>`; then its cards' titles, and each container's
   * title, subtitle and body (`message-blocks-in-chunks-*`).
   */
  #streamText(ts: string): string {
    const [shown, cards] = this.#shown(ts);
    // Python's `^` with re.M matches after "\n" only; JavaScript's also after "\r" and the
    // Unicode line separators, hence the lookbehind.
    const text = shown
      .replace(/\[([^\]]*)\]\(([^)]*)\)/g, "<$2|$1>")
      .replace(/\*\*([^\n]+?)\*\*/g, "*$1*")
      .replace(/(?<![^\n])#+ /g, "")
      .replace(/(?<![^\n])[-*] /g, "• ");
    const containers: string[] = [];
    for (const chunk of this.#message(ts).chunks) {
      if (chunk.type !== "blocks") continue;
      for (const item of list(need(chunk, "blocks"), "blocks")) {
        const block = object(item, "block");
        if (block.type !== "container") continue;
        const code = list(need(block, "child_blocks"), "child_blocks").flatMap((child) =>
          list(need(object(child, "child"), "elements"), "elements").flatMap((pre) =>
            list(need(object(pre, "pre"), "elements"), "elements").map(
              (e) => `\`\`\`${str(need(object(e, "element"), "text"))}\`\`\``,
            ),
          ),
        );
        const subtitle = block.subtitle === undefined ? {} : object(block.subtitle, "subtitle");
        containers.push(
          [
            str(need(object(need(block, "title"), "title"), "text")),
            str(subtitle.text ?? ""),
            ...code,
          ].join(" "),
        );
      }
    }
    return strip([text, ...cards.map((card) => str(card.title)), ...containers].join(" "));
  }

  #message(ts: string): FakeMessage {
    const message = this.messages.get(ts);
    if (message === undefined) throw new Error(`no message ${ts}`);
    return message;
  }

  /**
   * What `conversations.replies` reads back of the messages this fake holds: the daemon's own,
   * as its bot wrote them (a stream carries `streaming_state`), newer than `oldest`.
   */
  #thread(args: Args): JsonObject {
    const oldest = Number(args.oldest || 0);
    const found: JsonObject[] = [];
    for (const message of this.messages.values()) {
      if (!(Number(message.ts) > oldest) || message.deleted) continue;
      found.push({
        ts: message.ts,
        user: BOT,
        type: "message",
        text: message.text || this.#streamText(message.ts),
        blocks: message.blocks,
        ...(this.streamTs.includes(message.ts)
          ? { streaming_state: message.streaming ? "in_progress" : "completed" }
          : {}),
      });
    }
    return { ok: true, messages: found, has_more: false };
  }

  /**
   * What a message shows now: its body text and its cards. A stream shows what its chunks say
   * until a `chat.update` replaces it with the blocks that update carried. A chunk replaces its
   * card's title and status, and adds its details and its output to those the card holds
   * (measured 2026-10-01 and 2026-10-08).
   */
  #shown(ts: string): [string, JsonObject[]] {
    const message = this.#message(ts);
    if (message.updated || message.chunks.length === 0) {
      const text = message.blocks
        .filter((block) => block.type === "markdown")
        .map((block) => str(need(block, "text")))
        .join("\n\n");
      const cards = message.blocks.filter((b) => b.type === "task_card").map(cardOf);
      return [text, cards];
    }
    const text = message.chunks
      .filter((chunk) => chunk.type === "markdown_text")
      .map((chunk) => str(need(chunk, "text")))
      .join("");
    const cards = new Map<string, JsonObject>();
    for (const chunk of message.chunks) {
      if (chunk.type !== "task_update") continue;
      const card = cardOf(chunk);
      const key = str(need(chunk, "id"));
      const held = cards.get(key) ?? {};
      for (const field of ["details", "output"]) {
        if (field in held || field in card) {
          card[field] = str(held[field] ?? "") + str(card[field] ?? "");
        }
      }
      cards.set(key, card);
    }
    return [strip(text), [...cards.values()]];
  }

  /** The text each message shows last (Claude's words, as paragraphs), in the order the messages
   * were created, posts and streams alike. */
  messageTexts(): string[] {
    return this.createdTs.map((ts) => this.#shown(ts)[0]);
  }

  /** The text of the messages that began as streams, in the order they started: a reply's own
   * messages, with no approval request or notice among them. */
  streamTexts(): string[] {
    return this.streamTs.map((ts) => this.#shown(ts)[0]);
  }

  /** The task cards each message shows last, as plain objects, in creation order. */
  messageCards(): JsonObject[][] {
    return this.createdTs.map((ts) => this.#shown(ts)[1]);
  }

  /** The blocks each message shows last (a stream's are the ones its stop wrote), in creation
   * order. */
  messageBlocks(): JsonObject[][] {
    return this.createdTs.map((ts) => this.#message(ts).blocks);
  }

  /** The arguments of every call to `method`, in order. */
  callsTo(method: string): Args[] {
    return this.apiCalls.filter((call) => call.method === method).map((call) => call.args);
  }

  /**
   * How many times Slack would notify the owner of a thread the owner started: a new message
   * that is a post, and a stream when it stops (measured 2026-09-29: nothing at its start),
   * whichever way it stops (the daemon's stop, or Slack's at the stream's end of life). An edit
   * never does. Posts count only those of a reply: an approval is a post too.
   */
  pushes(): number {
    return this.postedTs.length + this.streamEnds;
  }
}

/**
 * Slack applied the call, then the connection reset before the answer came back: the daemon
 * cannot tell whether it took effect. `resetNext` names the method that does it once.
 */
export class ResetAfterApply extends FakeSlack {
  resetNext: string | null = null;

  override async apiCall(
    method: string,
    options: Record<string, unknown> = {},
  ): Promise<WebAPICallResult> {
    const answer = await super.apiCall(method, options);
    if (method === this.resetNext) {
      this.resetNext = null;
      throw connectionReset();
    }
    return answer;
  }
}

/**
 * Slack applies the call, and its answer takes `slowFor` seconds more to come back: a task
 * cancelled in that wait loses what the call did.
 */
export class SlowAfterApply extends FakeSlack {
  slowMethod: string | null = null;
  slowFor = 0;

  override async apiCall(
    method: string,
    options: Record<string, unknown> = {},
  ): Promise<WebAPICallResult> {
    const answer = await super.apiCall(method, options);
    if (method === this.slowMethod) await sleep(this.slowFor * 1000);
    return answer;
  }
}

interface Sleeper {
  readonly due: number;
  wake(): void;
}

/**
 * The stream deadline's clock: a sleep ends when `advance` reaches its time, so a test crosses
 * the 280 seconds without waiting. `advance` wakes the sleepers whose time has come in the order
 * they began to sleep, whatever their times, then lets what woke run to its next wait.
 */
export class FakeClock implements Clock {
  now = 0;
  #sleepers: Sleeper[] = [];

  sleep(seconds: number, signal?: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(signal.reason);
        return;
      }
      const onAbort = () => {
        this.#sleepers = this.#sleepers.filter((other) => other !== sleeper);
        reject(signal?.reason);
      };
      const sleeper: Sleeper = {
        due: this.now + seconds,
        wake: () => {
          signal?.removeEventListener("abort", onAbort);
          resolve();
        },
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      this.#sleepers.push(sleeper);
    });
  }

  time(): number {
    return this.now;
  }

  async advance(seconds: number): Promise<void> {
    this.now += seconds;
    const due = this.#sleepers.filter((sleeper) => sleeper.due <= this.now);
    this.#sleepers = this.#sleepers.filter((sleeper) => sleeper.due > this.now);
    for (const sleeper of due) sleeper.wake();
    // Let what woke run to its next wait: a turn of the event loop drains every microtask.
    for (let round = 0; round < 20; round += 1) {
      await new Promise<void>((resolve) => setImmediate(resolve));
    }
  }
}
