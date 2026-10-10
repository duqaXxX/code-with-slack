/** Load the daemon's configuration from ~/.config/awaydesk/.env. */
import { lstatSync, readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join } from "node:path";
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

/** The variables a `${NAME}` may name besides the file's own; the process environment by default. */
export type Env = Readonly<Record<string, string | undefined>>;

// python-dotenv's own pattern (`dotenv/variables.py`, 1.2.4): `${NAME}` and `${NAME:-default}`.
// `$NAME` without braces, a lone `$` and `${NAME:other}` stay as they are.
const VARIABLE = /\$\{([^}:]*)(?::-([^}]*))?\}/g;

/**
 * The key names of `text` in the order the file defines them. `util.parseEnv` returns its keys
 * sorted, which loses the order python-dotenv resolves in: a name defined later in the file does
 * not exist yet for the values before it. A key `parseEnv` read that no line start names (should
 * a syntax differ) goes last, so no key is dropped.
 */
function fileOrder(text: string, keys: readonly string[]): string[] {
  const wanted = new Set(keys);
  const ordered = new Set<string>();
  for (const [, key] of text.matchAll(/^[ \t]*(?:export[ \t]+)?([^\s=#]+)[ \t]*=/gm)) {
    if (key !== undefined && wanted.has(key)) ordered.add(key);
  }
  return [...ordered, ...keys.filter((key) => !ordered.has(key))];
}

/**
 * The values of a `.env` text with `${NAME}` and `${NAME:-default}` expanded as python-dotenv does
 * by default (`interpolate=True`, which this file was read with before). A name resolves from the
 * values the file defined above it, else from `env`, else to its default, else to nothing; an
 * `env` entry that is empty counts as set. The expanded value is final: nothing in it is expanded
 * again. Quotes were dropped by `parseEnv` and python-dotenv expands inside single quotes too, so
 * every value is expanded alike. Not ported: a key repeated in the file (the last value is used
 * and expanded once, where python-dotenv expands each occurrence) and a bare `NAME` line with no
 * `=` (python-dotenv records it as unset and `${NAME:-d}` then gives nothing; here `d`).
 */
export function expandEnv(text: string, env: Env = process.env): Record<string, string> {
  const raw = new Map(Object.entries(parseEnv(text)));
  const resolved = new Map<string, string>();
  for (const key of fileOrder(text, [...raw.keys()])) {
    const value = raw.get(key);
    if (value === undefined) continue;
    resolved.set(
      key,
      value.replace(VARIABLE, (_match, name: string, fallback: string | undefined) => {
        const defined = resolved.get(name);
        if (defined !== undefined) return defined;
        const inherited = Object.hasOwn(env, name) ? env[name] : undefined;
        return inherited ?? fallback ?? "";
      }),
    );
  }
  return Object.fromEntries(resolved);
}

/**
 * `~` and `~/x` to the home directory, as `Path.expanduser` does (`~user` is not expanded). Joined
 * as text: `path.join` would fold a `link/..` that the disk has to follow first.
 */
function expandUser(path: string): string {
  if (path !== "~" && !path.startsWith("~/")) return path;
  return homedir().replace(/\/+$/, "") + path.slice(1) || "/";
}

/**
 * Absolute, symlinks resolved as far as the path exists, as `Path.resolve()` does. Made absolute
 * as text and never normalized, since `resolve` would fold `link/..` before the link is followed
 * and name another folder than the one the kernel reaches; `realpathSync.native` is libuv's
 * `realpath(3)`, which follows it, where the JavaScript `realpathSync` normalizes first. What does
 * not exist is kept, with the `..` in it folded, as Python does.
 */
function resolvePath(path: string): string {
  const absolute = isAbsolute(path) ? path : `${process.cwd()}/${path}`;
  const rest: string[] = [];
  let head = absolute;
  for (;;) {
    try {
      return join(realpathSync.native(head), ...rest);
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
export function loadConfig(configDir: string = CONFIG_DIR, env: Env = process.env): Config {
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
  for (const [key, raw] of Object.entries(expandEnv(text, env))) {
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
