/**
 * The account's usage limits, read from `/usage` on a session of their own: the limits belong to
 * the account, and a probe inside a channel's session would add to that session's transcript.
 */
import { randomUUID } from "node:crypto";
import { type Clock, systemClock } from "../../clock.ts";
import type {
  AgentBackend,
  AgentSession,
  PermissionAnswer,
  QuestionAnswer,
  RequestHandler,
  SessionEvent,
} from "../seam.ts";
import { parseUsage, USAGE_TIMEOUT, type Usage } from "./usage.ts";

const REFUSAL = "The usage probe asks for nothing.";

// `/usage` runs inside Claude Code and asks nothing; should it ever, the answer is no.
const NO_ANSWERS: RequestHandler = {
  permission: async (): Promise<PermissionAnswer> => ({ allow: false, message: REFUSAL }),
  question: async (): Promise<QuestionAnswer> => ({ answered: false, message: REFUSAL }),
};

export interface UsageProbeOptions {
  readonly clock?: Clock;
  /** Milliseconds one `read` may take, start included. */
  readonly timeout?: number;
}

export class UsageProbe {
  readonly #folder: string;
  readonly #start: AgentBackend["start"];
  readonly #clock: Clock;
  readonly #timeout: number;
  #session: AgentSession | null = null;
  #events: AsyncIterator<SessionEvent> | null = null;
  // Counts the closes, so a session still starting when one happens is closed as it arrives.
  #epoch = 0;

  /** `start` is the back end's: the probe session takes no setting source of the owner's. */
  constructor(folder: string, start: AgentBackend["start"], options: UsageProbeOptions = {}) {
    this.#folder = folder;
    this.#start = start;
    this.#clock = options.clock ?? systemClock;
    this.#timeout = options.timeout ?? USAGE_TIMEOUT;
  }

  /**
   * The limits `/usage` reports now. One timeout covers the whole call; on any failure, the
   * timeout included, the session is closed and the failure rethrown: a session left mid-turn
   * would answer the next call late.
   */
  async read(): Promise<Usage> {
    const limit = new AbortController();
    // Settles only by running out: the sleep's own rejection is its cancellation.
    const expiry = new Promise<never>((_, reject) => {
      this.#clock.sleep(this.#timeout / 1000, limit.signal).then(
        () => reject(new DOMException("the usage probe had no answer in time", "TimeoutError")),
        () => undefined,
      );
    });
    try {
      return await Promise.race([this.#ask(), expiry]);
    } catch (error) {
      await this.close();
      throw error;
    } finally {
      limit.abort();
    }
  }

  /** Closes the probe's session, if one is open; the next `read` starts another. */
  async close(): Promise<void> {
    this.#epoch += 1;
    const session = this.#session;
    this.#session = null;
    this.#events = null;
    if (session !== null) await session.close();
  }

  async #ask(): Promise<Usage> {
    const epoch = this.#epoch;
    if (this.#session === null) {
      const session = await this.#start(
        {
          folder: this.#folder,
          resume: null,
          settingsSources: [],
          model: null,
          effort: null,
          permissionMode: null,
        },
        NO_ANSWERS,
      );
      if (epoch !== this.#epoch) {
        await session.close();
        throw new Error("the usage probe was closed while it started");
      }
      this.#session = session;
      this.#events = session.events[Symbol.asyncIterator]();
    }
    const session = this.#session;
    const events = this.#events;
    if (events === null) throw new Error("the usage probe was closed");
    await session.send({ id: randomUUID(), content: "/usage" });
    for (;;) {
      const next = await events.next();
      if (next.done) throw new Error("the usage probe's session ended");
      const event = next.value;
      if (event.type === "turn_ended") {
        return parseUsage(event.finalText ?? "", this.#clock.time() * 1000);
      }
      if (event.type === "process_lost") throw new Error("the usage probe lost its process");
    }
  }
}
