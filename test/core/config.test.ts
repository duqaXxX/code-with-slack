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
import { ConfigError, expandEnv, loadConfig } from "../../src/core/config.ts";

const POSIX_ONLY = { skip: process.platform === "win32" };

const BOT = `${"xox"}b-000-fake`;
const APP = `${"xap"}p-000-fake`;

const made: string[] = [];

function scratch(): string {
  const directory = realpathSync.native(mkdtempSync(join(tmpdir(), "awd-config-")));
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

// Review round 2026-10-10: `ALLOWED_ROOT` is made absolute as text and resolved on the disk, so a
// `..` after a link is followed from where the link leads, as `Path.resolve()` does.

/** `root/link` leads to `elsewhere/deep`; Python resolves `root/link/..` to `elsewhere`. */
function linkLayout(): string {
  const directory = scratch();
  mkdirSync(join(directory, "root"));
  mkdirSync(join(directory, "elsewhere", "deep"), { recursive: true });
  symlinkSync(join("..", "elsewhere", "deep"), join(directory, "root", "link"));
  return directory;
}

test("a root with dot dot after a link is followed from the link s target", POSIX_ONLY, () => {
  const directory = linkLayout();
  // Written as text: `join` would fold the `..` before the disk is asked.
  writeEnv(directory, `${directory}/root/link/..`);
  assert.equal(loadConfig(directory).allowedRoot, join(directory, "elsewhere"));
});

test(
  "a tilde root with dot dot after a link is followed from the link s target",
  POSIX_ONLY,
  () => {
    const directory = linkLayout();
    const home = process.env.HOME;
    process.env.HOME = join(directory, "root");
    try {
      writeEnv(directory, "~/link/..");
      assert.equal(loadConfig(directory).allowedRoot, join(directory, "elsewhere"));
    } finally {
      if (home === undefined) delete process.env.HOME;
      else process.env.HOME = home;
    }
  },
);

test("a root whose tail does not exist keeps the link s target", POSIX_ONLY, () => {
  // Python: `root/link/../nothere` resolves to `elsewhere/nothere`; that folder is absent, so the
  // root is refused with the resolved path in the message.
  const directory = linkLayout();
  writeEnv(directory, `${directory}/root/link/../nothere`);
  assert.throws(
    () => loadConfig(directory),
    (error: unknown) =>
      error instanceof ConfigError &&
      error.message ===
        `ALLOWED_ROOT is not a directory: ${join(directory, "elsewhere", "nothere")}`,
  );
});

test("a root whose missing part is followed by dot dot folds it as Python does", POSIX_ONLY, () => {
  // Python: `root/missing/../deep` resolves to `root/deep`.
  const directory = linkLayout();
  mkdirSync(join(directory, "root", "deep"));
  writeEnv(directory, `${directory}/root/missing/../deep`);
  assert.equal(loadConfig(directory).allowedRoot, join(directory, "root", "deep"));
});

// Review round 2026-10-10: python-dotenv, which read this file before, expands `${NAME}` and
// `${NAME:-default}` by default. The sample below is a scratch file read by python-dotenv 1.2.4
// (`dotenv_values`, interpolate on) with the process environment set to PROC below; each expected
// value is what it returned. A name resolves from the file's earlier values, then the process
// environment, then its default, then to nothing; a `$` that is not `${...}` stays.
const PROC: Record<string, string> = {
  HOME: "/h",
  EMPTY_PROC: "",
  PROC_ONLY: "fromproc",
  EARLIER: "procearlier",
  LATER: "proclater",
};

const DOTENV_SAMPLE = `A_HOME=\${HOME}/code
EARLIER=one
USES_EARLIER=\${EARLIER}-x
USES_LATER=\${LATER}-y
LATER=two
UNSET=\${NOPE_NOT_SET}|
DEF=\${NOPE_NOT_SET:-d}|
DEF_SET=\${EARLIER:-d}|
DEF_EMPTY=\${EMPTY_PROC:-d}|
NOBRACE=$HOME/x
DOLLAR=tok$en$
DOLLAR2=a$
SQ='\${HOME}/sq'
DQ="\${HOME}/dq"
BADCOLON=\${HOME:x}
NESTED=\${A:-\${HOME}}
EMPTYNAME=\${}|
EMPTYDEFNAME=\${:-zz}|
SELF=\${SELF}x
PROTO=\${constructor}|\${__proto__}|\${toString:-t}|
MULTI=\${HOME}\${HOME}
SPACE= \${HOME} 
ESC=\\\${HOME}
FROM_PROC=\${PROC_ONLY}!\${EARLIER}
export EXPORTED=\${HOME}/e
LAST_KEY=\${HOME}
`;

const DOTENV_EXPECTED: Record<string, string> = {
  A_HOME: "/h/code",
  EARLIER: "one",
  USES_EARLIER: "one-x",
  USES_LATER: "proclater-y",
  LATER: "two",
  UNSET: "|",
  DEF: "d|",
  DEF_SET: "one|",
  DEF_EMPTY: "|",
  NOBRACE: "$HOME/x",
  DOLLAR: "tok$en$",
  DOLLAR2: "a$",
  SQ: "/h/sq",
  DQ: "/h/dq",
  // biome-ignore lint/suspicious/noTemplateCurlyInString: a literal `${...}` of a .env value
  BADCOLON: "${HOME:x}",
  // biome-ignore lint/suspicious/noTemplateCurlyInString: a literal `${...}` of a .env value
  NESTED: "${HOME}",
  EMPTYNAME: "|",
  EMPTYDEFNAME: "zz|",
  SELF: "x",
  PROTO: "||t|",
  MULTI: "/h/h",
  SPACE: "/h",
  ESC: "\\/h",
  FROM_PROC: "fromproc!one",
  EXPORTED: "/h/e",
  LAST_KEY: "/h",
};

for (const [key, expected] of Object.entries(DOTENV_EXPECTED)) {
  test(`an env value is expanded as python-dotenv does [${key}]`, () => {
    assert.equal(expandEnv(DOTENV_SAMPLE, PROC)[key], expected);
  });
}

test("an env is expanded to the keys python-dotenv returned", () => {
  assert.deepEqual(expandEnv(DOTENV_SAMPLE, PROC), DOTENV_EXPECTED);
});

test("the root is read from the process environment by default", POSIX_ONLY, () => {
  const directory = scratch();
  mkdirSync(join(directory, "code"));
  // biome-ignore lint/suspicious/noTemplateCurlyInString: a literal `${...}` of a .env value
  writeEnv(directory, "${HOME}/code");
  assert.equal(loadConfig(directory, { HOME: directory }).allowedRoot, join(directory, "code"));
});

test("a root named by a variable defined earlier in the file", () => {
  const directory = scratch();
  mkdirSync(join(directory, "code"));
  const env = writeEnv(directory, directory);
  writeFileSync(env, `BASE=${directory}\n${readFileSync(env, "utf8")}`, { mode: 0o600 });
  writeFileSync(
    env,
    // biome-ignore lint/suspicious/noTemplateCurlyInString: a literal `${...}` of a .env value
    readFileSync(env, "utf8").replace(/^ALLOWED_ROOT=.*$/m, "ALLOWED_ROOT=${BASE}/code"),
  );
  assert.equal(loadConfig(directory, {}).allowedRoot, join(directory, "code"));
});

test("expanding an env never touches the process environment", () => {
  const before = { ...process.env };
  expandEnv(DOTENV_SAMPLE, PROC);
  assert.deepEqual({ ...process.env }, before);
});
