/** Why a session cannot start, or stopped under whoever was using it. */
import * as texts from "../texts.ts";

/** The channel's directory cannot be used; `message` tells the owner what to do. */
export class DirectoryUnavailable extends Error {
  readonly directory: string;

  constructor(directory: string, message: string) {
    super(message);
    this.name = "DirectoryUnavailable";
    this.directory = directory;
  }
}

export class DirectoryMissing extends DirectoryUnavailable {
  constructor(directory: string) {
    super(directory, texts.fill(texts.DIRECTORY_MISSING, { directory }));
    this.name = "DirectoryMissing";
  }
}

/**
 * The owner has not trusted the folder in the agent: a session would run its hooks and apply
 * its settings with no trust dialog.
 */
export class DirectoryUntrusted extends DirectoryUnavailable {
  constructor(directory: string) {
    super(directory, texts.fill(texts.DIRECTORY_UNTRUSTED, { directory }));
    this.name = "DirectoryUntrusted";
  }
}

/**
 * macOS privacy protection (TCC) denies the daemon a folder such as ~/Documents: a process
 * started by launchd does not inherit the Terminal's permission, and the CLI fails to start.
 */
export class DirectoryUnreadable extends DirectoryUnavailable {
  constructor(directory: string) {
    super(directory, texts.fill(texts.DIRECTORY_UNREADABLE, { directory }));
    this.name = "DirectoryUnreadable";
  }
}

/**
 * The session closed (the daemon stopping, or the idle close, most likely) while something was
 * using it: a daemon word (`!status`, `!bypass`), or `submit` queuing an owner's prompt (the
 * caller retries once, against a freshly looked-up session).
 */
export class SessionClosed extends Error {
  constructor() {
    super(texts.SESSION_CLOSED);
    this.name = "SessionClosed";
  }
}

/**
 * A thread's stored session id could not be resumed: its entry was removed, and the thread
 * needs a new top-level message to start again.
 */
export class SessionGone extends Error {
  constructor(options?: ErrorOptions) {
    super(texts.SESSION_GONE, options);
    this.name = "SessionGone";
  }
}
