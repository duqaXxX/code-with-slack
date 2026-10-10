/**
 * Who may talk to the daemon, and in which channels it may answer.
 *
 * Every inbound path calls these on its own: a button is never trusted because of the message it
 * sits on, and the channel is re-read from Slack each time, since membership can change.
 */
import { WebAPIPlatformError, type WebClient } from "@slack/web-api";
import * as texts from "../../../core/texts.ts";

/** Where a line goes; ids and error names only, never message content. */
export interface Logger {
  warning(message: string): void;
}

export const logger: Logger = {
  warning: (message) => console.error(`WARNING chat.slack.app.guards: ${message}`),
};

/** A Slack payload: JSON the library or a recording parsed. */
export type Payload = Readonly<Record<string, unknown>>;

export interface Identity {
  readonly ownerUserId: string;
  readonly teamId: string;
  readonly botUserId: string;
}

/** The user and the workspace of an actor; null where the payload names none. */
export type Actor = readonly [user: string | null, team: string | null];

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function record(value: unknown): Payload {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Payload)
    : {};
}

/** True only for the configured owner acting from the workspace read at startup. */
export function isOwner(
  identity: Identity,
  userId: string | null | undefined,
  teamId: string | null | undefined,
): boolean {
  return Boolean(userId) && userId === identity.ownerUserId && teamId === identity.teamId;
}

/**
 * The user and the workspace of a message, `body` being the envelope the event came in. Two
 * events carry no `team`. A file_share (measured 2026-09-25): its files name the uploader's
 * workspace, and must all name the same one. A thread_broadcast, a reply also sent to the
 * channel (recorded 2026-10-09): the envelope's `team_id` stands in, and only when the envelope
 * says the channel is not shared outside the workspace, since that id names where the event
 * happened and not the writer's own workspace. One that carries files follows the files' rule,
 * the stricter of the two.
 */
export function messageActor(event: Payload, body: Payload): Actor {
  let team = text(event.team);
  const files = event.files;
  const hasFiles = Array.isArray(files) && files.length > 0;
  if (team === null && hasFiles) {
    const teams = new Set(files.map((file) => text(record(file).user_team)));
    const [only] = teams;
    team = teams.size === 1 && only !== undefined ? only : null;
  } else if (
    team === null &&
    event.subtype === "thread_broadcast" &&
    body.is_ext_shared_channel === false
  ) {
    team = text(body.team_id);
  }
  return [text(event.user), team];
}

/** The user and the workspace of a click; a user whose home team differs counts as foreign. */
export function interactionActor(body: Payload): Actor {
  const user = record(body.user);
  const team = text(record(body.team).id);
  const home = text(user.team_id);
  return [text(user.id), home === null || home === team ? team : null];
}

// The subtypes a message a person wrote can carry: a message with files, and a thread reply also
// sent to the channel. Any other subtype is an edit, a deletion or a join.
export const PROMPT_SUBTYPES: readonly string[] = ["file_share", "thread_broadcast"];

/** Python's `str.isspace` for one UTF-16 unit; `String.prototype.trim` differs at U+001C to U+001F, U+0085 and U+FEFF. */
function isSpace(code: number): boolean {
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
}

function hasText(value: unknown): boolean {
  return typeof value === "string" && [...value].some((char) => !isSpace(char.charCodeAt(0)));
}

/**
 * A message a person wrote: with no subtype or one of PROMPT_SUBTYPES, from no bot, with some
 * text or a file.
 */
export function isPromptMessage(event: Payload): boolean {
  // An absent subtype passes; a subtype that is present and null does not.
  const subtype = "subtype" in event ? event.subtype : "file_share";
  return (
    event.type === "message" &&
    typeof subtype === "string" &&
    PROMPT_SUBTYPES.includes(subtype) &&
    !("bot_id" in event) &&
    (hasText(event.text) || (Array.isArray(event.files) && event.files.length > 0))
  );
}

/** What went wrong reading a channel, as a log line says it: Slack's error code, or the error's name. */
function failure(error: unknown): string {
  if (error instanceof WebAPIPlatformError) return error.data.error;
  return error instanceof Error ? error.name : typeof error;
}

const SHARED_FLAGS = ["is_shared", "is_ext_shared", "is_org_shared", "is_pending_ext_shared"];

export class ChannelGuard {
  readonly #slack: WebClient;
  readonly #identity: Identity;
  readonly #log: Logger;

  constructor(slack: WebClient, identity: Identity, log: Logger = logger) {
    this.#slack = slack;
    this.#identity = identity;
    this.#log = log;
  }

  /** Null when the channel is private, unshared and holds exactly the owner and the bot. */
  async refusal(channelId: string): Promise<string | null> {
    let info: Payload;
    let members: { members?: string[]; response_metadata?: { next_cursor?: string } };
    try {
      const answer = await this.#slack.conversations.info({ channel: channelId });
      if (answer.channel === undefined) throw new Error("conversations.info answered no channel");
      info = answer.channel as Payload;
      members = await this.#slack.conversations.members({ channel: channelId, limit: 10 });
    } catch (error) {
      // A network failure too: an unread channel is never trusted.
      this.#log.warning(`could not read channel ${channelId}: ${failure(error)}`);
      return texts.REASON_UNREADABLE;
    }
    if (!info.is_private || info.is_im || info.is_mpim) return texts.REASON_NOT_PRIVATE;
    if (SHARED_FLAGS.some((flag) => info[flag])) return texts.REASON_SHARED;
    const more = members.response_metadata?.next_cursor;
    if (members.members === undefined) throw new Error("conversations.members answered no members");
    const present = new Set(members.members);
    const expected = new Set([this.#identity.ownerUserId, this.#identity.botUserId]);
    const same = present.size === expected.size && [...present].every((id) => expected.has(id));
    if (more || !same) return texts.REASON_MEMBERS;
    return null;
  }
}
