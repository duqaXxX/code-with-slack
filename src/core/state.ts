/**
 * The daemon's only written file: per channel, its bound directory and any pending migration
 * notice; per thread within it, the folder it was opened in, its session id, bypass switch and
 * effort level.
 */
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import { dirname, join } from "node:path";
import { getLogger } from "../log.ts";

export const logger = getLogger("awaydesk.core.state");

// A thread with no session id whose root message is older than this never finished its first
// turn: `prune` drops it. A Slack `thread_ts` is the root message's epoch time in seconds.
export const ONE_DAY = 24 * 60 * 60;

/** A `(channelId, threadTs)` pair. */
export type ThreadKey = readonly [channelId: string, threadTs: string];

/**
 * What an observer is told after a write: the threads it touched, as a set (no order, no
 * repeats).
 */
export type SessionsChange = (changed: readonly ThreadKey[]) => void;

export interface ThreadState {
  /** The folder the thread was opened in. */
  readonly directory: string;
  readonly sessionId: string | null;
  // The owner's last word on bypass, kept across restarts (a restart is the daemon's doing, not
  // the owner's): true after `!bypass on` or a ticked setup, false after `!bypass off` or an
  // unticked one, null when never chosen, which leaves Claude Code's own mode alone. On disk,
  // `bypass` keeps its old meaning (`true` on, `false` not on) and an explicit off adds
  // `bypass_off: true`, written for that alone. A file from before reads as never chosen where
  // it held `false`; old code ignores the extra key and reads an off as not on.
  readonly bypass: boolean | null;
  /** The level set with `/effort`; null when unset or set back to the default. */
  readonly effort: string | null;
  // Crash repair (issue #19), each an id only, never message content:
  // the ts of every open reply's last message (more than one can be open at once: a background
  // task's own reply can outlive the turn that started it). Each `ReplySink` owns one entry:
  // added on its first message, replaced on a continuation, removed once its final write is
  // known to have landed (or it has given up retrying for good).
  readonly openReplies: readonly string[];
  /** ts of every approval, question or same-folder hold request still carrying buttons. */
  readonly requests: readonly string[];
  // The root's reaction name while it is ⏳ or ✋ (`render.status.Status.value`); cleared once
  // ✅ or ❌ is requested.
  readonly status: string | null;
  // The root's reaction name once ✅ or ❌ is requested, cleared when it turns ⏳ or ✋ again:
  // what the Home tab shows for a session with nothing running. Never read by crash repair.
  // ❌ stands here next to a kept `status` when an answer never reached Slack: the root shows
  // ❌, and repair still owes the thread its notice.
  readonly ended: string | null;
}

export interface ChannelRecord {
  readonly directory: string;
  /** Set by the v1 migration, cleared once the migration notice is posted to the channel. */
  readonly noticePending: boolean;
  readonly threads: ReadonlyMap<string, ThreadState>;
}

/** A `ThreadState` with every field but the folder defaulted to "nothing set". */
export function threadState(
  directory: string,
  fields: Partial<Omit<ThreadState, "directory">> = {},
): ThreadState {
  return {
    directory,
    sessionId: null,
    bypass: null,
    effort: null,
    openReplies: [],
    requests: [],
    status: null,
    ended: null,
    ...fields,
  };
}

/** A `ChannelRecord` with no pending notice and no threads unless given. */
export function channelRecord(
  directory: string,
  fields: Partial<Omit<ChannelRecord, "directory">> = {},
): ChannelRecord {
  return { directory, noticePending: false, threads: new Map(), ...fields };
}

/** state.json exists but cannot be read; the daemon refuses to guess. */
export class StateError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "StateError";
  }
}

/** `openThread` was given a channel that has never been bound. */
export class UnknownChannel extends Error {
  constructor(channelId: string) {
    super(`unknown channel: ${channelId}`);
    this.name = "UnknownChannel";
  }
}

/** A part of the file has the wrong shape; the message becomes the detail of a `StateError`. */
class Malformed extends Error {}

type Raw = Record<string, unknown>;

function isRaw(value: unknown): value is Raw {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `value` as an object, or `Malformed` naming `what`. */
function asRaw(value: unknown, what: string): Raw {
  if (!isRaw(value)) throw new Malformed(`${what} must be an object`);
  return value;
}

/** The key's value; a missing key reads as Python's `KeyError` did: the key in quotes. */
function required(raw: Raw, key: string): unknown {
  if (!Object.hasOwn(raw, key)) throw new Malformed(`'${key}'`);
  return raw[key];
}

function requiredString(raw: Raw, key: string): string {
  const value = required(raw, key);
  if (typeof value !== "string") throw new Malformed(`'${key}' must be a string`);
  return value;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function stringList(value: unknown): readonly string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function parseBypass(raw: Raw): boolean | null {
  if (raw.bypass === true) return true;
  return raw.bypass_off === true ? false : null;
}

/** `bypass` as it always was (`true` on, `false` not on), plus `bypass_off: true` for an
 * explicit off only: never both set. */
function dumpBypass(bypass: boolean | null): Record<string, boolean> {
  if (bypass) return { bypass: true };
  return bypass === false ? { bypass: false, bypass_off: true } : { bypass: false };
}

/**
 * Tolerant of a v2 file written before the repair fields existed (they default to "nothing
 * open"), and a v2 file written with them read by code that does not know them yet ignores the
 * extra keys: both directions of the additive version stay 2 (`bypass_off` is one such key:
 * a file without it reads its `bypass: false` as never chosen; `ended` is another). A file
 * written by 1304c5e's single `open_reply` field (never shipped) is read the same as one with
 * none at all: that key is not looked at.
 */
export function parseThread(raw: Raw): ThreadState {
  return {
    directory: requiredString(raw, "directory"),
    sessionId: stringOrNull(raw.session_id),
    bypass: parseBypass(raw),
    effort: stringOrNull(raw.effort),
    openReplies: stringList(raw.open_replies),
    requests: stringList(raw.requests),
    status: stringOrNull(raw.status),
    ended: stringOrNull(raw.ended),
  };
}

function parseV2(raw: Raw): Map<string, ChannelRecord> {
  const channels = new Map<string, ChannelRecord>();
  for (const [channelId, rawEntry] of Object.entries(
    asRaw(required(raw, "channels"), "'channels'"),
  )) {
    const entry = asRaw(rawEntry, `channel ${channelId}`);
    const threads = new Map<string, ThreadState>();
    const rawThreads = Object.hasOwn(entry, "threads") ? entry.threads : {};
    for (const [threadTs, rawThread] of Object.entries(
      asRaw(rawThreads, `'threads' of ${channelId}`),
    )) {
      threads.set(threadTs, parseThread(asRaw(rawThread, `thread ${threadTs}`)));
    }
    channels.set(channelId, {
      directory: requiredString(entry, "directory"),
      noticePending: entry.notice_pending === true,
      threads,
    });
  }
  return channels;
}

/**
 * What the session index (the Home tab) shows of `threads`: a write that leaves this
 * unchanged is not announced to `StateStore.onSessionsChange`.
 */
function shown(threads: ReadonlyMap<string, ThreadState>): Map<string, string> {
  return new Map(
    [...threads].map(([ts, t]) => [ts, JSON.stringify([t.sessionId, t.status, t.ended])]),
  );
}

/**
 * Each channel keeps its directory, gets empty threads and a pending notice; the old session id
 * and bypass switch belonged to the channel, not to a thread, and are dropped.
 */
function migrateV1(raw: Raw): Map<string, ChannelRecord> {
  const channels = new Map<string, ChannelRecord>();
  for (const [channelId, rawEntry] of Object.entries(
    asRaw(required(raw, "channels"), "'channels'"),
  )) {
    const entry = asRaw(rawEntry, `channel ${channelId}`);
    channels.set(
      channelId,
      channelRecord(requiredString(entry, "directory"), { noticePending: true }),
    );
  }
  return channels;
}

/** The text Python's `json.dump(data, f, indent=2)` writes: ASCII only, no trailing newline. */
function dumpJson(data: unknown): string {
  // `JSON.stringify` differs from `ensure_ascii=True` only in leaving DEL and everything from
  // U+0080 up as they are; a UTF-16 code unit escaped on its own gives the same `🚀`
  // pair for an astral character that Python writes.
  return JSON.stringify(data, null, 2).replace(
    /[\u007f-￿]/g,
    (unit) => `\\u${unit.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

function threadKey(channelId: string, threadTs: string): string {
  return `${channelId}\0${threadTs}`;
}

export interface StateStoreOptions {
  /** Where a warning goes; ids and error names only, never message content. */
  warn?: (message: string) => void;
}

/** In-memory copy of state.json, written atomically on every change. */
export class StateStore {
  private readonly path: string;
  private readonly warn: (message: string) => void;
  private channelsById: Map<string, ChannelRecord>;
  // Called after a write that changed which channels are bound, which threads exist, the
  // session one holds, or a root's reaction: what the session index is built from. It is
  // given the (channelId, threadTs) of each thread the write touched. Never while loading.
  onSessionsChange: SessionsChange | null = null;

  constructor(path: string, options: StateStoreOptions = {}) {
    this.path = path;
    this.warn = options.warn ?? ((message) => logger.warning(message));
    this.channelsById = this.load();
  }

  channel(channelId: string): ChannelRecord | null {
    return this.channelsById.get(channelId) ?? null;
  }

  /** The id of every bound channel, in the order they were bound. */
  channels(): string[] {
    return [...this.channelsById.keys()];
  }

  thread(channelId: string, threadTs: string): ThreadState | null {
    return this.channelsById.get(channelId)?.threads.get(threadTs) ?? null;
  }

  /** Every (channelId, threadTs, thread), across all channels. */
  threads(): [string, string, ThreadState][] {
    return [...this.channelsById].flatMap(([channelId, channel]) =>
      [...channel.threads].map(([threadTs, thread]): [string, string, ThreadState] => [
        channelId,
        threadTs,
        thread,
      ]),
    );
  }

  /**
   * Bind a channel to a directory; its threads and their own folders stay untouched (each
   * thread keeps the folder it was created in), and so does its pending notice.
   */
  bind(channelId: string, directory: string): void {
    const current = this.channelsById.get(channelId);
    this.channelsById.set(channelId, {
      directory,
      noticePending: current?.noticePending ?? false,
      threads: current?.threads ?? new Map(),
    });
    this.save();
    if (current === undefined) this.announce(); // a new channel in the session index; a rebind changes no row
  }

  /** Forget a channel: its binding and every thread in it. A no-op for one that is not bound. */
  removeChannel(channelId: string): void {
    const channel = this.channelsById.get(channelId);
    if (channel === undefined) return;
    this.channelsById.delete(channelId);
    this.save();
    this.announce([...channel.threads.keys()].map((threadTs): ThreadKey => [channelId, threadTs]));
  }

  /**
   * Create a thread's entry with the channel's current folder, or return the existing one
   * unchanged. Throws `UnknownChannel` for a channel that has never been bound.
   */
  openThread(channelId: string, threadTs: string, sessionId: string | null = null): ThreadState {
    const channel = this.channelsById.get(channelId);
    if (channel === undefined) throw new UnknownChannel(channelId);
    const existing = channel.threads.get(threadTs);
    if (existing !== undefined) return existing;
    const created = threadState(channel.directory, { sessionId });
    this.replaceThreads(channelId, new Map([...channel.threads, [threadTs, created]]));
    return created;
  }

  /** Record a thread's session id; a no-op for a thread that does not exist. */
  setSession(channelId: string, threadTs: string, sessionId: string | null): void {
    const current = this.thread(channelId, threadTs);
    if (current !== null && current.sessionId !== sessionId) {
      this.setThread(channelId, threadTs, { ...current, sessionId });
    }
  }

  /**
   * Record a thread's bypass choice (null: never chosen); a no-op for a thread that does not
   * exist.
   */
  setBypass(channelId: string, threadTs: string, on: boolean | null): void {
    const current = this.thread(channelId, threadTs);
    if (current !== null && current.bypass !== on) {
      this.setThread(channelId, threadTs, { ...current, bypass: on });
    }
  }

  /** Record the effort level `/effort` set for a thread; a no-op for an unknown thread. */
  setEffort(channelId: string, threadTs: string, effort: string | null): void {
    const current = this.thread(channelId, threadTs);
    if (current !== null && current.effort !== effort) {
      this.setThread(channelId, threadTs, { ...current, effort });
    }
  }

  /**
   * One `ReplySink`'s own entry in the open-replies list (crash repair): drop `oldTs` (if it
   * was there), add `newTs` (if not already there), in one write. A no-op for a thread that
   * does not exist, and for a call that changes nothing (both ends of a fresh sink's first
   * message, `oldTs=null, newTs=null`, would otherwise still write).
   */
  replaceOpenReply(
    channelId: string,
    threadTs: string,
    oldTs: string | null,
    newTs: string | null,
  ): void {
    const current = this.thread(channelId, threadTs);
    if (current === null) return;
    let replies = current.openReplies;
    if (oldTs !== null && replies.includes(oldTs)) replies = replies.filter((t) => t !== oldTs);
    if (newTs !== null && !replies.includes(newTs)) replies = [...replies, newTs];
    if (
      replies.length !== current.openReplies.length ||
      replies.some((t, i) => t !== current.openReplies[i])
    ) {
      this.setThread(channelId, threadTs, { ...current, openReplies: replies });
    }
  }

  /**
   * Record a request message still carrying buttons (crash repair); a no-op for a thread that
   * does not exist or already has it.
   */
  addRequest(channelId: string, threadTs: string, messageTs: string): void {
    const current = this.thread(channelId, threadTs);
    if (current !== null && !current.requests.includes(messageTs)) {
      this.setThread(channelId, threadTs, {
        ...current,
        requests: [...current.requests, messageTs],
      });
    }
  }

  /**
   * Drop a decided or removed request from the list (crash repair); a no-op if it is not there,
   * including for a thread that does not exist.
   */
  removeRequest(channelId: string, threadTs: string, messageTs: string): void {
    const current = this.thread(channelId, threadTs);
    if (current?.requests.includes(messageTs)) {
      const remaining = current.requests.filter((t) => t !== messageTs);
      this.setThread(channelId, threadTs, { ...current, requests: remaining });
    }
  }

  /**
   * Record the root's reaction while it is ⏳ or ✋ (`name`), or clear it once ✅ or ❌ is
   * requested (crash repair) and keep that one as `ended`; both null for a root left bare, both
   * set for a ❌ shown while repair still owes the thread. A no-op for a thread that does not
   * exist.
   */
  setStatusPending(
    channelId: string,
    threadTs: string,
    name: string | null,
    ended: string | null = null,
  ): void {
    const current = this.thread(channelId, threadTs);
    if (current !== null && (current.status !== name || current.ended !== ended)) {
      this.setThread(channelId, threadTs, { ...current, status: name, ended });
    }
  }

  /**
   * Every (channelId, threadTs, thread) whose crash-repair fields are not all empty: what a
   * crashed daemon left open for startup repair to find.
   */
  repairsPending(): [string, string, ThreadState][] {
    return this.threads().filter(
      ([, , thread]) =>
        thread.openReplies.length > 0 || thread.requests.length > 0 || thread.status !== null,
    );
  }

  /**
   * Clear a thread's crash-repair fields together, once it has been repaired (or repair failed
   * for good and was logged); a no-op if already clear or the thread is gone. With `keepOpen`
   * the open replies and the root's status stay: an answer never reached Slack, and the next
   * start's repair still has to close it and say so.
   */
  clearRepair(
    channelId: string,
    threadTs: string,
    { keepOpen = false }: { keepOpen?: boolean } = {},
  ): void {
    const current = this.thread(channelId, threadTs);
    if (current === null) return;
    const replies = keepOpen ? current.openReplies : [];
    const status = keepOpen ? current.status : null;
    if (
      current.requests.length === 0 &&
      replies.length === current.openReplies.length &&
      current.status === status
    ) {
      return;
    }
    this.setThread(channelId, threadTs, {
      ...current,
      openReplies: replies,
      requests: [],
      status,
    });
  }

  /** Drop a thread's entry; a no-op if it is not there. */
  removeThread(channelId: string, threadTs: string): void {
    const channel = this.channelsById.get(channelId);
    if (channel === undefined || !channel.threads.has(threadTs)) return;
    const remaining = new Map(channel.threads);
    remaining.delete(threadTs);
    this.replaceThreads(channelId, remaining);
  }

  /** The (channelId, threadTs) whose thread holds this session id, across all channels. */
  holder(sessionId: string): ThreadKey | null {
    for (const [channelId, channel] of this.channelsById) {
      for (const [threadTs, thread] of channel.threads) {
        if (thread.sessionId === sessionId) return [channelId, threadTs];
      }
    }
    return null;
  }

  /** Channel ids whose v1-to-v2 migration notice has not been posted yet. */
  pendingNotices(): string[] {
    return [...this.channelsById].filter(([, c]) => c.noticePending).map(([id]) => id);
  }

  /** Mark a channel's migration notice as posted; a no-op if already clear or unbound. */
  clearNotice(channelId: string): void {
    const channel = this.channelsById.get(channelId);
    if (channel === undefined || !channel.noticePending) return;
    this.channelsById.set(channelId, { ...channel, noticePending: false });
    this.save();
  }

  /**
   * Remove a thread whose session id is gone from its folder's sessions, and a no-session
   * thread whose root message is older than `ONE_DAY`. Calls `alive` once per distinct folder;
   * null means it cannot tell, and that folder's threads are kept. `keep` names the
   * (channelId, threadTs) to leave alone whatever they look like: the threads with a live
   * session, whose id may not be on disk yet. If `alive` throws, nothing is removed or written;
   * the caller decides what to do next. Returns how many entries were removed.
   */
  prune(
    alive: (directory: string) => Iterable<string> | null,
    now: number,
    keep: Iterable<ThreadKey> = [],
  ): number {
    const kept = new Set([...keep].map(([channelId, threadTs]) => threadKey(channelId, threadTs)));
    const aliveCache = new Map<string, ReadonlySet<string> | null>();
    const updated = new Map<string, ChannelRecord>();
    let removed = 0;
    for (const [channelId, channel] of this.channelsById) {
      const survivors = new Map<string, ThreadState>();
      for (const [threadTs, thread] of channel.threads) {
        if (kept.has(threadKey(channelId, threadTs))) {
          survivors.set(threadTs, thread);
          continue;
        }
        if (thread.sessionId !== null) {
          if (!aliveCache.has(thread.directory)) {
            const sessions = alive(thread.directory);
            aliveCache.set(thread.directory, sessions === null ? null : new Set(sessions));
          }
          const sessions = aliveCache.get(thread.directory);
          if (sessions && !sessions.has(thread.sessionId)) {
            removed += 1;
            continue;
          }
        } else if (now - rootTime(threadTs) > ONE_DAY) {
          removed += 1;
          continue;
        }
        survivors.set(threadTs, thread);
      }
      updated.set(
        channelId,
        survivors.size === channel.threads.size ? channel : { ...channel, threads: survivors },
      );
    }
    if (removed > 0) {
      const gone: ThreadKey[] = [];
      for (const [channelId, channel] of this.channelsById) {
        const after = updated.get(channelId)?.threads;
        for (const threadTs of channel.threads.keys()) {
          if (!after?.has(threadTs)) gone.push([channelId, threadTs]);
        }
      }
      this.channelsById = updated;
      this.save();
      this.announce(gone);
    }
    return removed;
  }

  private setThread(channelId: string, threadTs: string, updated: ThreadState): void {
    const channel = this.channelsById.get(channelId) as ChannelRecord;
    this.replaceThreads(channelId, new Map([...channel.threads, [threadTs, updated]]));
  }

  private replaceThreads(channelId: string, threads: Map<string, ThreadState>): void {
    const channel = this.channelsById.get(channelId) as ChannelRecord;
    this.channelsById.set(channelId, { ...channel, threads });
    this.save();
    const before = shown(channel.threads);
    const after = shown(threads);
    const changed: ThreadKey[] = [];
    for (const threadTs of new Set([...before.keys(), ...after.keys()])) {
      if (before.get(threadTs) !== after.get(threadTs)) changed.push([channelId, threadTs]);
    }
    if (changed.length > 0) this.announce(changed);
  }

  /** The write is already on disk: an observer that fails must not fail its caller. */
  private announce(changed: readonly ThreadKey[] = []): void {
    if (this.onSessionsChange === null) return;
    try {
      this.onSessionsChange(changed);
    } catch (error) {
      const name = error instanceof Error ? error.constructor.name : typeof error;
      this.warn(`the session index was not told of a change: ${name}`);
    }
  }

  private load(): Map<string, ChannelRecord> {
    let bytes: Buffer;
    try {
      bytes = fs.readFileSync(this.path);
    } catch (error) {
      if (error instanceof Error && "code" in error && error.code === "ENOENT") return new Map();
      throw error;
    }
    let channels: Map<string, ChannelRecord>;
    let migrated = false;
    try {
      // Python's `read_text` stops on bytes that are not UTF-8 and keeps a BOM, which the JSON
      // parser then refuses; `TextDecoder` does the same with these two options.
      const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
      const raw = asRaw(JSON.parse(text), "the file");
      const version = required(raw, "version");
      if (version === 2) {
        channels = parseV2(raw);
      } else if (version === 1) {
        channels = migrateV1(raw);
        migrated = true;
      } else {
        throw new StateError(`${this.path} has an unknown version; fix or delete it`);
      }
    } catch (error) {
      const unreadable =
        error instanceof SyntaxError ||
        error instanceof Malformed ||
        (error instanceof TypeError &&
          "code" in error &&
          error.code === "ERR_ENCODING_INVALID_ENCODED_DATA");
      if (!unreadable) throw error;
      throw new StateError(`${this.path} cannot be read (${error.message}); fix or delete it`, {
        cause: error,
      });
    }
    if (migrated) this.write(channels);
    return channels;
  }

  private save(): void {
    this.write(this.channelsById);
  }

  /**
   * Written synchronously, as Python's was, so two changes never interleave: the next line of the
   * caller runs with the file already renamed into place.
   */
  private write(channels: ReadonlyMap<string, ChannelRecord>): void {
    const data = {
      version: 2,
      channels: Object.fromEntries(
        [...channels].map(([channelId, c]) => [
          channelId,
          {
            directory: c.directory,
            notice_pending: c.noticePending,
            threads: Object.fromEntries(
              [...c.threads].map(([threadTs, t]) => [
                threadTs,
                {
                  directory: t.directory,
                  session_id: t.sessionId,
                  ...dumpBypass(t.bypass),
                  effort: t.effort,
                  open_replies: [...t.openReplies],
                  requests: [...t.requests],
                  status: t.status,
                  ended: t.ended,
                },
              ]),
            ),
          },
        ]),
      ),
    };
    // Write beside the target and rename: a crash leaves the old file or the new one.
    const tmp = join(dirname(this.path), `.state-${randomBytes(4).toString("hex")}.tmp`);
    const fd = fs.openSync(tmp, "wx", 0o600);
    try {
      try {
        fs.writeFileSync(fd, dumpJson(data));
        fs.fsyncSync(fd);
      } finally {
        fs.closeSync(fd);
      }
      fs.renameSync(tmp, this.path);
    } catch (error) {
      try {
        fs.unlinkSync(tmp);
      } catch (unlinkError) {
        if (
          !(unlinkError instanceof Error && "code" in unlinkError && unlinkError.code === "ENOENT")
        ) {
          throw unlinkError;
        }
      }
      throw error;
    }
    // The rename is a directory entry: only a sync of the directory makes it survive a power cut.
    if (process.platform !== "win32") {
      const dirFd = fs.openSync(dirname(this.path), "r");
      try {
        fs.fsyncSync(dirFd);
      } finally {
        fs.closeSync(dirFd);
      }
    }
  }
}

/** A thread's root time in epoch seconds; throws on a `thread_ts` that is no number, as `float()` did. */
function rootTime(threadTs: string): number {
  const seconds = threadTs.trim() === "" ? Number.NaN : Number(threadTs);
  if (Number.isNaN(seconds)) throw new RangeError(`not a thread_ts: ${threadTs}`);
  return seconds;
}
