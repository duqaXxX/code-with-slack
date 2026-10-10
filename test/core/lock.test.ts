import assert from "node:assert/strict";
import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { pathToFileURL } from "node:url";
import {
  AlreadyRunning,
  CLAIM_STALE_MS,
  listenLocal,
  singleInstance,
  socketLock,
} from "../../src/core/lock.ts";

const LOCK_MODULE = pathToFileURL(join(import.meta.dirname, "..", "..", "src", "core", "lock.ts"));
const POSIX_ONLY = { skip: process.platform === "win32" };
// The directory lock is the macOS mechanism; the others run the socket one.
const MACOS_ONLY = {
  skip: process.platform === "darwin" ? false : "the directory lock is the macOS mechanism",
};
const HAS_PYTHON =
  process.platform === "darwin" &&
  spawnSync("python3", ["-c", "import fcntl"], { stdio: "ignore" }).status === 0;
const PYTHON_FLOCK = {
  skip: HAS_PYTHON ? false : "needs macOS and a python3 on the path",
  timeout: 20000,
};

const made: string[] = [];

/**
 * A fresh directory for a lock. A Unix socket path is limited to 104 bytes on macOS (103
 * usable), whose own `os.tmpdir()` is far longer than that, so on POSIX it lives under `/tmp`.
 */
function scratch(): string {
  const directory =
    process.platform === "win32" ? mkdtempSync(join(tmpdir(), "awd-")) : mkdtempSync("/tmp/awd-");
  made.push(directory);
  return directory;
}

after(() => {
  for (const directory of made) rmSync(directory, { recursive: true, force: true });
});

/**
 * A child process that prints `held` once `script` has run and then waits to be killed. The
 * child's stdout is read to the first `held`, never by a pause.
 */
async function child(command: string, args: string[]): Promise<ChildProcess> {
  const spawned = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
  let stderr = "";
  spawned.stderr?.on("data", (chunk: Buffer) => {
    stderr += chunk.toString();
  });
  await new Promise<void>((resolve, reject) => {
    let out = "";
    spawned.stdout?.on("data", (chunk: Buffer) => {
      out += chunk.toString();
      if (out.includes("held\n")) resolve();
    });
    spawned.once("exit", (code, signal) =>
      reject(new Error(`the child exited (${code ?? signal}) before holding: ${stderr}`)),
    );
  }).catch((error: unknown) => {
    spawned.kill("SIGKILL");
    throw error;
  });
  return spawned;
}

/** A Node child that takes the lock on `directory` with `taker`, prints `held` and waits. */
function holder(directory: string, taker: "singleInstance" | "socketLock"): Promise<ChildProcess> {
  const script = `
    const { ${taker} } = await import(${JSON.stringify(LOCK_MODULE.href)});
    await ${taker}(${JSON.stringify(directory)});
    console.log("held");
    setTimeout(() => {}, 30000);
  `;
  return child(process.execPath, ["--input-type=module", "-e", script]);
}

/** A Python child that holds `flock(LOCK_EX | LOCK_NB)` on `directory`, as the Python daemon does. */
function pythonHolder(directory: string): Promise<ChildProcess> {
  const script = [
    "import fcntl, os, sys, time",
    "fcntl.flock(os.open(sys.argv[1], os.O_RDONLY), fcntl.LOCK_EX | fcntl.LOCK_NB)",
    "print('held', flush=True); time.sleep(30)",
  ].join("\n");
  return child("python3", ["-c", script, directory]);
}

/** What Python's `flock(LOCK_EX | LOCK_NB)` on `directory` answers now: "refused" or "got it". */
function pythonTries(directory: string): string {
  const script = [
    "import fcntl, os, sys",
    "try: fcntl.flock(os.open(sys.argv[1], os.O_RDONLY), fcntl.LOCK_EX | fcntl.LOCK_NB)",
    "except BlockingIOError: print('refused')",
    "else: print('got it')",
  ].join("\n");
  return spawnSync("python3", ["-c", script, directory], { encoding: "utf8" }).stdout.trim();
}

/** Resolves once `child` has exited, whatever its state now. */
function exited(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve) => child.once("exit", () => resolve()));
}

async function killed(child: ChildProcess): Promise<void> {
  child.kill("SIGKILL");
  await exited(child);
}

test("a second holder is refused", async () => {
  const directory = scratch();
  const first = await singleInstance(directory);
  try {
    await assert.rejects(
      singleInstance(directory),
      (error: unknown) =>
        error instanceof AlreadyRunning &&
        error.message === `another awaydesk is running (lock on ${directory})`,
    );
  } finally {
    await first.release();
  }
});

test("the lock is released on exit", async () => {
  const directory = scratch();
  await (await singleInstance(directory)).release();
  await (await singleInstance(directory)).release();
});

test("another process is refused", { timeout: 20000 }, async () => {
  const directory = scratch();
  const other = await holder(directory, "singleInstance");
  try {
    await assert.rejects(singleInstance(directory), AlreadyRunning);
  } finally {
    await killed(other);
  }
});

// Where the lock is the socket (every platform but macOS) the lock does add a file, so this one
// runs on macOS alone; the socket's own file is tested below.
test("the lock creates no file", MACOS_ONLY, async () => {
  const directory = scratch();
  const lock = await singleInstance(directory);
  try {
    assert.deepEqual(readdirSync(directory), []);
  } finally {
    await lock.release();
  }
});

// Review round 2026-10-10: the macOS lock is Python's own `flock` on the directory.

test("Python's flock on the directory refuses the TypeScript lock", PYTHON_FLOCK, async () => {
  const directory = scratch();
  const python = await pythonHolder(directory);
  try {
    await assert.rejects(
      singleInstance(directory),
      (error: unknown) =>
        error instanceof AlreadyRunning &&
        error.message === `another awaydesk is running (lock on ${directory})`,
    );
  } finally {
    await killed(python);
  }
  // The kernel drops Python's flock with its process: the next start gets in.
  await (await singleInstance(directory)).release();
});

test("the TypeScript lock refuses Python's flock", PYTHON_FLOCK, async () => {
  const directory = scratch();
  const lock = await singleInstance(directory);
  try {
    assert.equal(pythonTries(directory), "refused");
  } finally {
    await lock.release();
  }
  assert.equal(pythonTries(directory), "got it");
});

test("the TypeScript lock of another process refuses Python's flock", PYTHON_FLOCK, async () => {
  const directory = scratch();
  const other = await holder(directory, "singleInstance");
  try {
    assert.equal(pythonTries(directory), "refused");
  } finally {
    await killed(other);
  }
  // A killed process cannot clean up, and needs no cleanup: the kernel drops the lock.
  assert.equal(pythonTries(directory), "got it");
  assert.deepEqual(readdirSync(directory), []);
});

test("releasing the lock twice is harmless", MACOS_ONLY, async () => {
  const directory = scratch();
  const lock = await singleInstance(directory);
  await lock.release();
  const next = await singleInstance(directory);
  try {
    // The second release must not close the descriptor the next lock now holds.
    await lock.release();
    await assert.rejects(singleInstance(directory), AlreadyRunning);
  } finally {
    await next.release();
  }
});

// The socket mechanism, called directly so that it is tested on macOS too.

test("the socket lock refuses a second holder", POSIX_ONLY, async () => {
  const directory = scratch();
  const first = await socketLock(directory);
  try {
    await assert.rejects(
      socketLock(directory),
      (error: unknown) =>
        error instanceof AlreadyRunning &&
        error.message === `another awaydesk is running (lock on ${directory})`,
    );
  } finally {
    await first.release();
  }
  await (await socketLock(directory)).release();
});

test("the socket lock of another process is refused", {
  ...POSIX_ONLY,
  timeout: 20000,
}, async () => {
  const directory = scratch();
  const other = await holder(directory, "socketLock");
  try {
    await assert.rejects(socketLock(directory), AlreadyRunning);
  } finally {
    await killed(other);
  }
});

test("the socket lock leaves only its socket, and release removes it", POSIX_ONLY, async () => {
  const directory = scratch();
  const lock = await socketLock(directory);
  assert.deepEqual(readdirSync(directory), ["lock.sock"]);
  await lock.release();
  assert.deepEqual(readdirSync(directory), []);
});

test("the socket is private to the owner", POSIX_ONLY, async () => {
  const directory = scratch();
  const lock = await socketLock(directory);
  try {
    assert.equal(statSync(join(directory, "lock.sock")).mode & 0o777, 0o600);
  } finally {
    await lock.release();
  }
});

test("a socket left by a dead process does not block the next start", {
  ...POSIX_ONLY,
  timeout: 20000,
}, async () => {
  const directory = scratch();
  await killed(await holder(directory, "socketLock"));
  // A killed process cannot clean up: its socket file is still there.
  assert.ok(existsSync(join(directory, "lock.sock")));
  const lock = await socketLock(directory);
  await lock.release();
  assert.deepEqual(readdirSync(directory), []);
});

// A claim file is stale by its mtime, whatever it holds: a recovery takes milliseconds, so one
// older than CLAIM_STALE_MS belongs to a starter that died (review 2026-10-10).

/** A clock that reads `afterMs` past the mtime of `path`. */
function clockPast(path: string, afterMs: number): () => number {
  const mtime = statSync(path).mtimeMs;
  return () => mtime + afterMs;
}

for (const [label, content] of [
  ["empty", ""],
  ["naming a live process", String(process.pid)],
  ["naming no process", "not a pid"],
] as const) {
  test(`a stale claim does not block the next start [${label}]`, POSIX_ONLY, async () => {
    const directory = scratch();
    const claim = join(directory, "lock.sock.claim");
    writeFileSync(claim, content);
    const lock = await socketLock(directory, { now: clockPast(claim, CLAIM_STALE_MS + 1) });
    await lock.release();
    assert.deepEqual(readdirSync(directory), []);
  });

  test(`a claim younger than the stale age refuses the start [${label}]`, POSIX_ONLY, async () => {
    const directory = scratch();
    const claim = join(directory, "lock.sock.claim");
    writeFileSync(claim, content);
    await assert.rejects(
      socketLock(directory, { now: clockPast(claim, CLAIM_STALE_MS) }),
      AlreadyRunning,
    );
    // The refused start leaves the claim of whoever holds it.
    assert.ok(existsSync(claim));
    assert.deepEqual(readdirSync(directory), ["lock.sock.claim"]);
  });
}

test("a stale claim and a dead socket together do not block the next start", {
  ...POSIX_ONLY,
  timeout: 20000,
}, async () => {
  const directory = scratch();
  await killed(await holder(directory, "socketLock"));
  const claim = join(directory, "lock.sock.claim");
  writeFileSync(claim, "");
  const lock = await socketLock(directory, { now: clockPast(claim, CLAIM_STALE_MS + 1) });
  await lock.release();
  assert.deepEqual(readdirSync(directory), []);
});

test("a start between another's claim and its first listen is refused", {
  ...POSIX_ONLY,
  timeout: 20000,
}, async () => {
  // A socket that is bound and not yet listening refuses connections, as the file of a dead
  // holder does: a second starter that probed it would take it for stale and remove it. The dead
  // holder's file stands in for it, and the second start runs inside the first one's `listen`,
  // after its claim and before it holds anything.
  const directory = scratch();
  await killed(await holder(directory, "socketLock"));
  const socket = join(directory, "lock.sock");
  let calls = 0;
  const first = await socketLock(directory, {
    listen: async (address) => {
      if (calls++ === 0) {
        await assert.rejects(socketLock(directory), AlreadyRunning);
        assert.ok(existsSync(socket), "the second start must leave the socket alone");
      }
      return listenLocal(address);
    },
  });
  try {
    assert.equal(calls, 2);
    assert.deepEqual(readdirSync(directory), ["lock.sock"]);
    await assert.rejects(socketLock(directory), AlreadyRunning);
  } finally {
    await first.release();
  }
});

test("two starts racing for a stale socket leave exactly one holder", POSIX_ONLY, async () => {
  const directory = scratch();
  await killed(await holder(directory, "socketLock"));
  const results = await Promise.allSettled([socketLock(directory), socketLock(directory)]);
  const won = results.filter((r) => r.status === "fulfilled");
  const lost = results.filter((r) => r.status === "rejected");
  try {
    assert.equal(won.length, 1);
    assert.equal(lost.length, 1);
    assert.ok(lost[0]?.status === "rejected" && lost[0].reason instanceof AlreadyRunning);
  } finally {
    for (const result of won) await result.value.release();
  }
});

test("two starts racing in a clean directory leave exactly one holder", POSIX_ONLY, async () => {
  const directory = scratch();
  const results = await Promise.allSettled([
    socketLock(directory),
    socketLock(directory),
    socketLock(directory),
  ]);
  const won = results.filter((r) => r.status === "fulfilled");
  try {
    assert.equal(won.length, 1);
    for (const result of results) {
      if (result.status === "rejected") assert.ok(result.reason instanceof AlreadyRunning);
    }
  } finally {
    for (const result of won) await result.value.release();
  }
});

test(
  "a path over the system's socket limit is refused with the limit named",
  POSIX_ONLY,
  async () => {
    const directory = join(scratch(), "x".repeat(150));
    mkdirSync(directory);
    await assert.rejects(
      socketLock(directory),
      (error: unknown) =>
        error instanceof Error &&
        !(error instanceof AlreadyRunning) &&
        /bytes.*limit/.test(error.message),
    );
  },
);
