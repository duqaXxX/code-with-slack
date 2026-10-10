/**
 * The agent seam: what the core asks of an agent back end and what a back end tells the core.
 * No type of an agent SDK appears here. One back end exists, `agent/claude`, and it is the only
 * module that imports the SDK.
 */

/** A permission mode, by the name the back end gives it. */
export type PermissionMode = string;

/** The modes a back end knows, by role. A role it lacks is null. */
export interface PermissionModes {
  readonly default: PermissionMode;
  readonly auto: PermissionMode | null;
  /** The mode that skips permission requests. */
  readonly bypass: PermissionMode | null;
}

/** What a back end supports. The core shows a feature only when its capability is present. */
export interface Capabilities {
  /** Calls of a subagent arrive with their parent call. */
  readonly subagents: boolean;
  /** Task events and `stopTask`. */
  readonly backgroundTasks: boolean;
  readonly compaction: boolean;
  readonly effort: boolean;
  /** `setEffort` changes the live session; without it the core restarts the session. */
  readonly liveEffort: boolean;
  readonly permissionModes: PermissionModes;
  /** An allow can rewrite the call's input. */
  readonly changedInput: boolean;
  readonly questions: boolean;
  readonly usageLimits: boolean;
  readonly models: boolean;
  readonly commands: boolean;
  /** A prompt sent while a turn runs is taken by the agent; without it the core queues it. */
  readonly promptDuringTurn: boolean;
  readonly sessionListing: boolean;
}

export type SettingsSource = "user" | "project" | "local";

export interface StartOptions {
  readonly folder: string;
  /** The session to resume, or null for a new one. */
  readonly resume: string | null;
  readonly settingsSources: readonly SettingsSource[];
  readonly model: string | null;
  readonly effort: string | null;
  readonly permissionMode: PermissionMode | null;
}

export interface TextPart {
  readonly type: "text";
  readonly text: string;
}

export interface ImagePart {
  readonly type: "image";
  readonly mediaType: string;
  /** Base64. */
  readonly data: string;
}

/** The owner's text, or the parts of one message when it carries images. */
export type PromptContent = string | readonly (TextPart | ImagePart)[];

export interface Prompt {
  /** The prompt's own id, a UUID the core makes, which `prompt_taken` names. */
  readonly id: string;
  readonly content: PromptContent;
}

/** What the agent says of its context window; a value it does not report is null. */
export interface ContextUsage {
  readonly model: string | null;
  /** The share of the context window in use, 0 to 100. */
  readonly percentage: number | null;
}

export interface ModelInfo {
  readonly value: string;
  /** The agent's name for the model, or its value when it gives none. */
  readonly displayName: string;
  readonly description: string | null;
  readonly supportsEffort: boolean;
  readonly supportedEffortLevels: readonly string[];
}

export interface CommandInfo {
  readonly name: string;
  readonly description: string;
  readonly argumentHint: string;
  readonly aliases: readonly string[];
}

export interface AgentInfo {
  readonly models: readonly ModelInfo[];
  readonly commands: readonly CommandInfo[];
  /** The mode the session started in, or null when the agent does not say. */
  readonly permissionMode: PermissionMode | null;
}

export interface ListedSession {
  readonly id: string;
  /** What the agent's own picker shows for the session. */
  readonly title: string;
  /** The title the owner gave it or the agent generated, which `!resume <title>` matches. */
  readonly customTitle: string | null;
  readonly branch: string | null;
  /** Bytes of the transcript. */
  readonly size: number | null;
  /** Milliseconds since the epoch. */
  readonly lastModified: number;
}

export interface DiffHunk {
  /** The number of the hunk's first line in the file as it was, and as it is now. */
  readonly oldStart: number;
  readonly newStart: number;
  /**
   * Each line with its sign as the first character: `-` removed, `+` added, a space for a
   * line that did not change, a backslash for a note about the line above.
   */
  readonly lines: readonly string[];
}

/** What a call did to a file, when the agent reports it. */
export interface FileChange {
  /** As the agent names it, absolute in the Claude back end. */
  readonly path: string;
  /** `edited` always has a hunk; `created` has the file's content. */
  readonly kind: "created" | "edited";
  readonly hunks: readonly DiffHunk[];
  /** A created file's content. */
  readonly content: string | null;
}

export type TaskKind = "subagent" | "command" | "other";
export type TurnEnding = "done" | "interrupted" | "error";

export interface ModelTokens {
  readonly input: number;
  readonly output: number;
  readonly cacheRead: number;
  readonly cacheCreation: number;
}

/**
 * What a back end tells the core. An event can arrive while no turn of the owner's runs:
 * `turn_ended` is an event like the others, not the answer to `send`.
 *
 * `parentCallId` names the call a subagent's event runs under, and is null at the top level.
 * The events of one message arrive in the order the agent produced them.
 */
export type SessionEvent =
  /** The agent names its session. It can come again, and the id can change (a cleared conversation). */
  | {
      readonly type: "session_started";
      readonly sessionId: string | null;
      readonly agentVersion: string | null;
    }
  /** The agent starts writing a message; its texts follow as `text_started` and `text_delta`. */
  | {
      readonly type: "message_started";
      readonly messageId: string | null;
      readonly parentCallId: string | null;
    }
  | {
      readonly type: "message_ended";
      readonly messageId: string | null;
      readonly parentCallId: string | null;
    }
  /** A text of the message being written starts; a message can hold several. */
  | {
      readonly type: "text_started";
      readonly messageId: string | null;
      readonly parentCallId: string | null;
    }
  /** The next piece of the text being written, never empty. */
  | {
      readonly type: "text_delta";
      readonly messageId: string | null;
      readonly text: string;
      readonly parentCallId: string | null;
    }
  /**
   * The whole text of a message that was not written piece by piece (a command's own output),
   * trimmed and never empty. A text reaches the core once: as deltas or whole, never both.
   */
  | {
      readonly type: "text";
      readonly messageId: string;
      readonly text: string;
      readonly parentCallId: string | null;
    }
  | {
      readonly type: "call_started";
      readonly callId: string;
      readonly toolName: string;
      /** The input as the agent sent it. */
      readonly input: Readonly<Record<string, unknown>>;
      readonly parentCallId: string | null;
    }
  | {
      readonly type: "call_ended";
      readonly callId: string;
      readonly isError: boolean;
      /** The result's text, as the agent wrote it; null when the result holds none. */
      readonly output: string | null;
      readonly fileChange: FileChange | null;
      readonly parentCallId: string | null;
    }
  /** A task starts: the work of a call (`callId`), or of nothing the core saw (null). */
  | {
      readonly type: "task_started";
      readonly taskId: string;
      readonly kind: TaskKind;
      /** The agent's own word for the kind, as written; null when it gives none. */
      readonly taskType: string | null;
      readonly description: string;
      readonly callId: string | null;
    }
  | {
      readonly type: "task_progress";
      readonly taskId: string;
      readonly description: string;
      readonly callId: string | null;
    }
  /**
   * A task's state changed. `terminal` says the task is over; the agent's report of the end
   * (`task_ended`) may follow, come first, or never come.
   */
  | {
      readonly type: "task_updated";
      readonly taskId: string;
      readonly status: string | null;
      readonly terminal: boolean;
    }
  /** The agent reports a task's end to the conversation. */
  | {
      readonly type: "task_ended";
      readonly taskId: string;
      /**
       * `completed`, `failed`, `killed` or `stopped` in the Claude back end. Any other word
       * reads as an end with no failure and is shown as written.
       */
      readonly status: string;
      readonly summary: string;
      readonly durationMs: number | null;
      readonly callId: string | null;
    }
  /** The agent is compacting the conversation. Not repeated while it lasts. */
  | { readonly type: "compaction_started" }
  /** It no longer is: `result` is the agent's word for how it went, null when it gives none. */
  | { readonly type: "compaction_ended"; readonly result: string | null }
  /** The conversation continues from its summary. A count the agent does not give is null. */
  | {
      readonly type: "compacted";
      readonly tokensBefore: number | null;
      readonly tokensAfter: number | null;
    }
  | { readonly type: "mode_changed"; readonly mode: PermissionMode }
  /** The effort the agent runs at; null is its default. */
  | { readonly type: "effort_observed"; readonly level: string | null }
  | { readonly type: "folder_changed"; readonly folder: string }
  /** The cached usage limits are stale. */
  | { readonly type: "limits_changed" }
  /** The agent took a prompt the core sent: into the running turn, or as the next turn's. */
  | { readonly type: "prompt_taken"; readonly promptId: string }
  | {
      readonly type: "turn_ended";
      readonly sessionId: string | null;
      readonly startedBy: "owner" | "agent";
      readonly finalText: string | null;
      readonly ending: TurnEnding;
      /** By model. Empty when the turn used none (a command the agent ran itself). */
      readonly tokens: Readonly<Record<string, ModelTokens>>;
    }
  /**
   * The agent wrote a message about a failed request of its own, in place of an answer.
   * `category` is its word for the failure, as written; `text` is what the message says, which
   * can be empty.
   */
  | {
      readonly type: "agent_error";
      readonly kind: "authentication" | "other";
      readonly category: string;
      readonly text: string;
      readonly parentCallId: string | null;
    }
  | { readonly type: "process_lost"; readonly reason: string };

export type SessionEventType = SessionEvent["type"];

export interface PermissionRequest {
  readonly type: "permission";
  readonly requestId: string;
  readonly callId: string | null;
  readonly toolName: string;
  readonly input: Readonly<Record<string, unknown>>;
  /** The agent's own sentence for the request, when it writes one. */
  readonly title: string | null;
  readonly description: string | null;
}

export type PermissionAnswer =
  | { readonly allow: true; readonly changedInput?: Readonly<Record<string, unknown>> }
  | { readonly allow: false; readonly message: string };

export interface QuestionOption {
  readonly label: string;
  readonly description: string | null;
  /** What picking the option would look like, in lines of the agent's own. */
  readonly preview: string | null;
}

export interface Question {
  /** A short title, empty when the agent gives none. */
  readonly header: string;
  /** The question, which also names it in the answer. */
  readonly text: string;
  readonly multiSelect: boolean;
  readonly options: readonly QuestionOption[];
}

export interface QuestionRequest {
  readonly type: "question";
  readonly requestId: string;
  readonly callId: string | null;
  readonly questions: readonly Question[];
}

/**
 * The answer to each question by its text: one answer, or the several of a multi-select. Or
 * skipped, with what the agent is told.
 */
export type QuestionAnswer =
  | {
      readonly answered: true;
      readonly answers: Readonly<Record<string, string | readonly string[]>>;
    }
  | { readonly answered: false; readonly message: string };

/** Asked by a back end while a call waits; each resolves once, for as long as it takes. */
export interface RequestHandler {
  permission(request: PermissionRequest): Promise<PermissionAnswer>;
  question(request: QuestionRequest): Promise<QuestionAnswer>;
}

/** One live session of an agent. */
export interface AgentSession {
  /** Every event of the session, in order, until the session closes or its process is lost. */
  readonly events: AsyncIterable<SessionEvent>;
  send(prompt: Prompt): Promise<void>;
  interrupt(): Promise<void>;
  stopTask(taskId: string): Promise<void>;
  setPermissionMode(mode: PermissionMode): Promise<void>;
  setModel(model: string | null): Promise<void>;
  setEffort(level: string | null): Promise<void>;
  contextUsage(): Promise<ContextUsage>;
  info(): Promise<AgentInfo>;
  close(): Promise<void>;
}

/** A folder's git repository, as the filesystem shows it. */
export interface Repository {
  /** The folder that holds the `.git` entry. */
  readonly root: string;
  /** The path whose trust covers it: the main checkout for a registered worktree. */
  readonly key: string;
  /** Null when the `.git` entry names none. */
  readonly gitDir: string | null;
  /** The folder is in the root's own `.git` directory, and has no work tree. */
  readonly insideGitDir: boolean;
}

export interface AgentBackend {
  readonly capabilities: Capabilities;
  start(options: StartOptions, requests: RequestHandler): Promise<AgentSession>;
  /** The sessions of `folder` alone, newest first. */
  listSessions(folder: string): Promise<readonly ListedSession[]>;
  /** Whether the owner trusted `folder` in the agent, as the agent itself would decide. */
  folderTrusted(folder: string): Promise<boolean>;
  /**
   * The repository holding `folder` when the daemon's own git may run in it: one the owner
   * trusted, or one inside `sessionFolder` when that folder is trusted. Null anywhere else.
   */
  trustedRepository(folder: string, sessionFolder: string): Promise<Repository | null>;
}
