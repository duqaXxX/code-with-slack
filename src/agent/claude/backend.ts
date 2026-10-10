/**
 * The Claude back end: what the core asks of an agent, answered over the Agent SDK and over
 * Claude Code's own records on disk. The only module a caller builds; the trust gate for a
 * session's start is the core's (`folderTrusted` is its question to ask), not this class's.
 */
import { homedir } from "node:os";
import type {
  AgentBackend,
  AgentSession,
  ListedSession,
  Repository,
  RequestHandler,
  StartOptions,
} from "../seam.ts";
import { CAPABILITIES } from "./capabilities.ts";
import { chromeEnabled } from "./chrome.ts";
import { aliveSessions, byLastActivity, directorySessions } from "./listing.ts";
import { ClaudeSession, type QueryFunction } from "./session.ts";
import { trustedRepository, workspaceTrusted } from "./trust.ts";

export interface ClaudeBackendOptions {
  /** The SDK's `query`; a test passes a fake. */
  readonly query?: QueryFunction;
  /** The owner's home, where Claude Code keeps its record of trust and of Chrome. */
  readonly home?: string;
}

export class ClaudeBackend implements AgentBackend {
  readonly capabilities = CAPABILITIES;
  readonly #query: QueryFunction | undefined;
  readonly #home: string;

  constructor(options: ClaudeBackendOptions = {}) {
    this.#query = options.query;
    this.#home = options.home ?? homedir();
  }

  /**
   * Starts Claude Code and returns once it answered. Rejects with `ResumeRefused` when it
   * refuses to resume `options.resume`, and with the failure itself when it cannot start.
   * Whether the owner trusted the folder is the caller's to check first (`folderTrusted`).
   */
  async start(options: StartOptions, requests: RequestHandler): Promise<AgentSession> {
    // Read at every start, so a change in `/chrome` holds from a session's next start.
    const chrome = await chromeEnabled(this.#home);
    const session = new ClaudeSession({ ...options, chrome }, requests, this.#query);
    await session.ready();
    return session;
  }

  /**
   * Starts a session for the daemon's own use, the usage probe's: the folder and no setting
   * source, without the permission to bypass and without the Chrome flag, which only a thread's
   * session is given (Python's probe built a bare client). Fails as `start` does.
   */
  async startBare(folder: string, requests: RequestHandler): Promise<AgentSession> {
    const session = new ClaudeSession(
      {
        folder,
        resume: null,
        settingsSources: [],
        model: null,
        effort: null,
        permissionMode: null,
        chrome: false,
        bare: true,
      },
      requests,
      this.#query,
    );
    await session.ready();
    return session;
  }

  /** The sessions of `folder` alone, newest first by their files' times. */
  listSessions(folder: string): Promise<readonly ListedSession[]> {
    return directorySessions(folder);
  }

  /**
   * `sessions` of `folder` by their last message, newest first, as the terminal's picker shows
   * them. Dating reads files, so a caller that shows only some of a folder's sessions passes
   * those alone.
   */
  datedSessions(folder: string, sessions: readonly ListedSession[]): Promise<ListedSession[]> {
    return byLastActivity(folder, sessions);
  }

  /** The session ids alive in `folder`, or null when it cannot tell (the pruning keeps them). */
  aliveSessions(folder: string): Promise<ReadonlySet<string> | null> {
    return aliveSessions(folder);
  }

  folderTrusted(folder: string): Promise<boolean> {
    return workspaceTrusted(folder, this.#home);
  }

  trustedRepository(folder: string, sessionFolder: string): Promise<Repository | null> {
    return trustedRepository(folder, sessionFolder, this.#home);
  }
}
