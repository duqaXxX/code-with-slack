/**
 * Forgetting what `state.json` no longer needs: a bound channel the chat no longer has, and a
 * thread whose session is gone. Run once on start and then every `CLEAN_EVERY_SECONDS`, so a daemon
 * that stays up for weeks cleans too.
 *
 * Only a certain answer removes anything. For a channel that is Slack's `channel_not_found` from
 * `conversations.info` (its reference, read 2026-10-01; measured the same day on deleted
 * channels), which is also what a private channel the bot was removed from answers: the two cannot
 * be told apart, and a channel forgotten by mistake is bound again with `!bind`. A rate limit, a
 * server error or the network is no answer and removes nothing. For a thread the rule is
 * `StateStore.prune`'s. The chat provider supplies the lookup that turns Slack's answer into a
 * `ChannelAnswer`.
 */
import type { StateStore, ThreadKey } from "./state.ts";

/** Where a line goes; ids and error names only, never message content. */
export interface Logger {
  info(message: string): void;
  warning(message: string): void;
}

export const logger: Logger = {
  info: (message) => console.error(`INFO core.cleanup: ${message}`),
  warning: (message) => console.error(`WARNING core.cleanup: ${message}`),
};

export const CLEAN_EVERY_SECONDS = 6 * 60 * 60;

/**
 * What the chat says of a bound channel: it is `gone` (a certain answer), it is `there`, or it
 * gave `no answer` (a rate limit, a server error, the network, a refused scope).
 */
export type ChannelAnswer = "gone" | "there" | "no answer";

/** Asks the chat about one channel by its id; a lookup that throws counts as `no answer`. */
export type ChannelLookup = (channelId: string) => Promise<ChannelAnswer>;

/**
 * The (channelId, threadTs) of every thread with a live session in the daemon, read at the moment
 * it is needed: nothing of theirs is removed under them.
 */
export type Live = () => Iterable<ThreadKey>;

/** A folder's session ids, or null when it cannot tell. */
export type Alive = (
  directory: string,
) => Iterable<string> | null | Promise<Iterable<string> | null>;

export interface CleanOptions {
  readonly alive: Alive;
  readonly live: Live;
  /** Seconds since the epoch. */
  readonly now?: () => number;
  readonly logger?: Logger;
}

/** The name a log line gives a failure: the errno code of a system error, else the error's name. */
function errorName(error: unknown): string {
  if (error instanceof Error) {
    const code = (error as { code?: unknown }).code;
    return typeof code === "string" ? code : error.name;
  }
  return typeof error;
}

/**
 * Remove from `state` each bound channel the chat answers `gone` about, with its threads, and
 * return their ids. A channel with a live session is left for the next pass. When the chat finds
 * none of the bound channels (a channel it gave no answer about is not one it found), nothing is
 * removed: that is what the token of another workspace, or an app removed from every channel,
 * looks like, and forgetting every binding would be the wrong repair.
 */
export async function forgetGoneChannels(
  lookup: ChannelLookup,
  state: StateStore,
  live: Live,
  log: Logger = logger,
): Promise<string[]> {
  const bound = state.channels();
  const gone: string[] = [];
  let found = 0;
  for (const channelId of bound) {
    let answer: ChannelAnswer;
    try {
      answer = await lookup(channelId);
    } catch (error) {
      log.warning(`could not read channel ${channelId}: ${errorName(error)}`);
      continue;
    }
    if (answer === "there") {
      found += 1;
    } else if (answer === "gone") {
      gone.push(channelId);
    } else {
      log.warning(`could not read channel ${channelId}: ${answer}`);
    }
  }
  if (gone.length === 0) return [];
  if (found === 0) {
    log.warning(
      `Slack finds none of the ${bound.length} bound channels: nothing is forgotten (check the ` +
        "workspace of the bot token, and that the bot is still in its channels)",
    );
    return [];
  }
  const busy = new Set([...live()].map(([channelId]) => channelId));
  const forgotten: string[] = [];
  for (const channelId of gone) {
    const record = state.channel(channelId);
    if (record === null || busy.has(channelId)) continue;
    state.removeChannel(channelId);
    forgotten.push(channelId);
    log.info(
      `forgot channel ${channelId}: Slack no longer has it (${record.threads.size} thread(s))`,
    );
  }
  return forgotten;
}

/**
 * One pass: forget the channels that are gone, then prune the threads whose session is (`alive`
 * gives a folder's session ids, or null when it cannot tell). Never throws: a pass that fails is
 * logged and the next one tries again.
 */
export async function clean(
  lookup: ChannelLookup,
  state: StateStore,
  options: CleanOptions,
): Promise<void> {
  const { alive, live } = options;
  const now = options.now ?? (() => Date.now() / 1000);
  const log = options.logger ?? logger;
  try {
    await forgetGoneChannels(lookup, state, live, log);
  } catch (error) {
    log.warning(`could not check the bound channels: ${errorName(error)}`);
  }
  try {
    const folders = new Set(
      state
        .threads()
        .filter(([, , thread]) => thread.sessionId !== null)
        .map(([, , thread]) => thread.directory),
    );
    // Reading a folder's sessions is file work that may wait: the state is touched only once all
    // of it is read, and a folder opened meanwhile has no entry here, so its threads are kept.
    const known = new Map<string, readonly string[] | null>();
    for (const folder of folders) {
      const sessions = await alive(folder);
      known.set(folder, sessions === null ? null : [...sessions]);
    }
    const removed = state.prune((directory) => known.get(directory) ?? null, now(), live());
    if (removed > 0) log.info(`pruned ${removed} stale thread(s) from state.json`);
  } catch (error) {
    log.warning(`could not prune stale threads: ${errorName(error)}`);
  }
}
