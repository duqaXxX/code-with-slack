/** Load the daemon's configuration from ~/.config/awaydesk/.env. */
import { lstatSync, readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { parseEnv } from "node:util";

export const CONFIG_DIR = join(homedir(), ".config", "awaydesk");
export const REQUIRED = [
  "SLACK_BOT_TOKEN",
  "SLACK_APP_TOKEN",
  "SLACK_OWNER_USER_ID",
  "ALLOWED_ROOT",
] as const;

/** The configuration is missing, unsafe or invalid; the message says what to fix. */
export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}

/**
 * The tokens are non-enumerable, so `util.inspect`, `JSON.stringify` and a log line that prints
 * the object never show them (Python's `repr=False`). A spread copy of a `Config` drops them.
 */
export interface Config {
  readonly botToken: string;
  readonly appToken: string;
  readonly ownerUserId: string;
  readonly allowedRoot: string;
  readonly configDir: string;
  /**
   * The owner's own user token, optional: what deleting a thread's messages that are the
   * owner's needs. null: the Home tab offers no delete.
   */
  readonly userToken: string | null;
}

function errorCode(error: unknown): string | undefined {
  return error instanceof Error && "code" in error && typeof error.code === "string"
    ? error.code
    : undefined;
}

/** `~` and `~/x` to the home directory, as `Path.expanduser` does (`~user` is not expanded). */
function expandUser(path: string): string {
  if (path === "~") return homedir();
  return path.startsWith("~/") ? join(homedir(), path.slice(2)) : path;
}

/** Absolute, symlinks resolved as far as the path exists, as `Path.resolve()` does. */
function resolvePath(path: string): string {
  const absolute = resolve(path);
  const rest: string[] = [];
  let head = absolute;
  for (;;) {
    try {
      return join(realpathSync(head), ...rest);
    } catch (error) {
      const code = errorCode(error);
      if (code !== "ENOENT" && code !== "ENOTDIR") throw error;
      const parent = dirname(head);
      if (parent === head) return absolute;
      rest.unshift(basename(head));
      head = parent;
    }
  }
}

function isDirectory(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch (error) {
    const code = errorCode(error);
    if (code === "ENOENT" || code === "ENOTDIR") return false;
    throw error;
  }
}

function hiddenTokens(
  config: Omit<Config, "botToken" | "appToken" | "userToken">,
  secrets: {
    botToken: string;
    appToken: string;
    userToken: string | null;
  },
): Config {
  return Object.defineProperties(config, {
    botToken: { value: secrets.botToken, enumerable: false },
    appToken: { value: secrets.appToken, enumerable: false },
    userToken: { value: secrets.userToken, enumerable: false },
  }) as Config;
}

/** Read and validate `.env`; throws ConfigError, never touches `process.env`. */
export function loadConfig(configDir: string = CONFIG_DIR): Config {
  const envPath = join(configDir, ".env");
  let st: ReturnType<typeof lstatSync>;
  try {
    st = lstatSync(envPath);
  } catch (error) {
    if (errorCode(error) === "ENOENT") {
      throw new ConfigError(`${envPath} does not exist; see docs/setup.md, Part 2`);
    }
    throw error;
  }
  if (!st.isFile()) {
    throw new ConfigError(`${envPath} must be a regular file, not a link`);
  }
  // Ownership and mode are POSIX facts; the daemon is not supported on Windows yet.
  if (process.platform !== "win32") {
    if (st.uid !== process.getuid?.()) {
      throw new ConfigError(`${envPath} must belong to the user running awaydesk`);
    }
    // The tokens drive a shell on this machine: nobody but the owner may read them.
    if (st.mode & 0o077) {
      throw new ConfigError(`${envPath} is readable by others; run: chmod 600 ${envPath}`);
    }
  }

  const values: Record<string, string> = {};
  // A byte order mark would become part of the first variable's name.
  const text = readFileSync(envPath, "utf8").replace(/^\ufeff/, "");
  for (const [key, raw] of Object.entries(parseEnv(text))) {
    const value = raw?.trim();
    if (value) values[key] = value;
  }
  const missing = REQUIRED.filter((key) => !(key in values));
  if (missing.length > 0) {
    throw new ConfigError(`missing in ${envPath}: ${missing.join(", ")}`);
  }
  const botToken = values.SLACK_BOT_TOKEN as string;
  const appToken = values.SLACK_APP_TOKEN as string;
  if (!botToken.startsWith("xoxb-")) {
    throw new ConfigError("SLACK_BOT_TOKEN must be the Bot User OAuth Token (xoxb-...)");
  }
  if (!appToken.startsWith("xapp-")) {
    throw new ConfigError("SLACK_APP_TOKEN must be the app-level token (xapp-...)");
  }
  const userToken = values.SLACK_USER_TOKEN ?? null;
  if (userToken !== null && !userToken.startsWith("xoxp-")) {
    throw new ConfigError("SLACK_USER_TOKEN must be the User OAuth Token (xoxp-...)");
  }
  const root = resolvePath(expandUser(values.ALLOWED_ROOT as string));
  if (!isDirectory(root)) {
    throw new ConfigError(`ALLOWED_ROOT is not a directory: ${root}`);
  }
  return hiddenTokens(
    {
      ownerUserId: values.SLACK_OWNER_USER_ID as string,
      allowedRoot: root,
      configDir,
    },
    { botToken, appToken, userToken },
  );
}
