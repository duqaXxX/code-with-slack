import assert from "node:assert/strict";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { inspect } from "node:util";
import { ConfigError, loadConfig } from "../../src/core/config.ts";

const POSIX_ONLY = { skip: process.platform === "win32" };

const BOT = `${"xox"}b-000-fake`;
const APP = `${"xap"}p-000-fake`;

const made: string[] = [];

function scratch(): string {
  const directory = realpathSync(mkdtempSync(join(tmpdir(), "awd-config-")));
  made.push(directory);
  return directory;
}

after(() => {
  for (const directory of made) rmSync(directory, { recursive: true, force: true });
});

/** Writes `.env` in `directory`; an empty override leaves its variable out. */
function writeEnv(
  directory: string,
  root: string,
  overrides: Record<string, string> = {},
  mode = 0o600,
): string {
  const values: Record<string, string> = {
    SLACK_BOT_TOKEN: BOT,
    SLACK_APP_TOKEN: APP,
    SLACK_OWNER_USER_ID: "U000ALICE",
    ALLOWED_ROOT: root,
    ...overrides,
  };
  const env = join(directory, ".env");
  writeFileSync(
    env,
    Object.entries(values)
      .filter(([, value]) => value)
      .map(([key, value]) => `${key}=${value}\n`)
      .join(""),
  );
  chmodSync(env, mode);
  return env;
}

test("loads a private env", () => {
  const directory = scratch();
  writeEnv(directory, directory);
  const config = loadConfig(directory);
  assert.equal(config.ownerUserId, "U000ALICE");
  assert.equal(config.allowedRoot, directory);
  assert.equal(config.configDir, directory);
});

test("reads an env that starts with a byte order mark", () => {
  // Some editors write one, and python-dotenv, which read this file before, strips it.
  const directory = scratch();
  const env = writeEnv(directory, directory);
  writeFileSync(env, `\ufeff${readFileSync(env, "utf8")}`, { mode: 0o600 });
  assert.equal(loadConfig(directory).botToken, BOT);
});

test("the owner s user token is optional and checked by its prefix", () => {
  const directory = scratch();
  writeEnv(directory, directory);
  assert.equal(loadConfig(directory).userToken, null);
  const user = `${"xox"}p-000-fake`;
  writeEnv(directory, directory, { SLACK_USER_TOKEN: user });
  const config = loadConfig(directory);
  assert.equal(config.userToken, user);
  assert.ok(!inspect(config).includes(user));
  writeEnv(directory, directory, { SLACK_USER_TOKEN: BOT });
  assert.throws(
    () => loadConfig(directory),
    (error: unknown) => error instanceof ConfigError && /SLACK_USER_TOKEN/.test(error.message),
  );
});

for (const mode of [0o640, 0o604, 0o660, 0o644]) {
  test(
    `refuses an env readable by others [${mode.toString(8).padStart(4, "0")}]`,
    POSIX_ONLY,
    () => {
      const directory = scratch();
      writeEnv(directory, directory, {}, mode);
      assert.throws(
        () => loadConfig(directory),
        (error: unknown) => error instanceof ConfigError && /chmod 600/.test(error.message),
      );
    },
  );
}

test("refuses a symlinked env", POSIX_ONLY, () => {
  const directory = scratch();
  const real = writeEnv(directory, directory);
  const linkDirectory = join(directory, "link");
  mkdirSync(linkDirectory);
  symlinkSync(real, join(linkDirectory, ".env"));
  assert.throws(
    () => loadConfig(linkDirectory),
    (error: unknown) => error instanceof ConfigError && /regular file/.test(error.message),
  );
});

test("missing env points to the setup guide", () => {
  assert.throws(
    () => loadConfig(scratch()),
    (error: unknown) => error instanceof ConfigError && /docs\/setup\.md/.test(error.message),
  );
});

test("names every missing variable", () => {
  const directory = scratch();
  writeEnv(directory, directory, { SLACK_APP_TOKEN: "", SLACK_OWNER_USER_ID: "" });
  assert.throws(
    () => loadConfig(directory),
    (error: unknown) =>
      error instanceof ConfigError &&
      error.message.includes("SLACK_APP_TOKEN, SLACK_OWNER_USER_ID"),
  );
});

test("rejects swapped tokens", () => {
  const directory = scratch();
  writeEnv(directory, directory, { SLACK_BOT_TOKEN: APP });
  assert.throws(
    () => loadConfig(directory),
    (error: unknown) => error instanceof ConfigError && /SLACK_BOT_TOKEN/.test(error.message),
  );
});

test("expands a tilde root", POSIX_ONLY, () => {
  // `os.homedir()` reads $HOME on POSIX, as Python's `Path.home()` does.
  const directory = scratch();
  mkdirSync(join(directory, "code"));
  const home = process.env.HOME;
  process.env.HOME = directory;
  try {
    writeEnv(directory, "~/code");
    assert.equal(loadConfig(directory).allowedRoot, join(directory, "code"));
  } finally {
    if (home === undefined) delete process.env.HOME;
    else process.env.HOME = home;
  }
});

test("rejects a root that is not a directory", () => {
  const directory = scratch();
  writeEnv(directory, join(directory, "nowhere"));
  assert.throws(
    () => loadConfig(directory),
    (error: unknown) => error instanceof ConfigError && /ALLOWED_ROOT/.test(error.message),
  );
});

test("repr never shows a token", () => {
  // The closest ways a TypeScript object reaches a log line: `inspect` and JSON.
  const directory = scratch();
  writeEnv(directory, directory);
  const config = loadConfig(directory);
  for (const text of [inspect(config), JSON.stringify(config), `${JSON.stringify(config)}`]) {
    assert.ok(!text.includes(BOT) && !text.includes(APP));
  }
  assert.equal(config.botToken, BOT);
  assert.equal(config.appToken, APP);
});

test("does not touch the process environment", () => {
  const directory = scratch();
  writeEnv(directory, directory);
  loadConfig(directory);
  assert.ok(!("SLACK_BOT_TOKEN" in process.env));
});
