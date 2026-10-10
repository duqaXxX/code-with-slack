import assert from "node:assert/strict";
import { type ChildProcess, spawn } from "node:child_process";
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
import { AlreadyRunning, singleInstance } from "../../src/core/lock.ts";

const LOCK_MODULE = pathToFileURL(join(import.meta.dirname, "..", "..", "src", "core", "lock.ts"));
const POSIX_ONLY = { skip: process.platform === "win32" };

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

/** A child process that takes the lock on `directory`, prints `held` and waits to be killed. */
async function holder(directory: string): Promise<ChildProcess> {
  const script = `
    const { singleInstance } = await import(${JSON.stringify(LOCK_MODULE.href)});
    await singleInstance(${JSON.stringify(directory)});
    console.log("held");
    setTimeout(() => {}, 30000);
  `;
  const child = spawn(process.execPath, ["--input-type=module", "-e", script], {
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr?.on("data", (chunk: Buffer) => {
    stderr += chunk.toString();
  });
  await new Promise<void>((resolve, reject) => {
    let out = "";
    child.stdout?.on("data", (chunk: Buffer) => {
      out += chunk.toString();
      if (out.includes("held\n")) resolve();
    });
    child.once("exit", (code, signal) =>
      reject(new Error(`the holder exited (${code ?? signal}) before holding: ${stderr}`)),
    );
  }).catch((error: unknown) => {
    child.kill("SIGKILL");
    throw error;
  });
  return child;
}

/** Resolves once `child` has exited, whatever its state now. */
function exited(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve) => child.once("exit", () => resolve()));
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
  const child = await holder(directory);
  try {
    await assert.rejects(singleInstance(directory), AlreadyRunning);
  } finally {
    child.kill("SIGKILL");
    await exited(child);
  }
});

// Not ported: `the lock creates no file`. The Python lock was an `flock` on the directory itself
// and added nothing to it; the port listens on `lock.sock` inside it, so the behaviour is
// replaced by the two tests around it: the socket is the only file and release removes it.

test("the lock leaves only its socket, and release removes it", POSIX_ONLY, async () => {
  const directory = scratch();
  const lock = await singleInstance(directory);
  assert.deepEqual(readdirSync(directory), ["lock.sock"]);
  await lock.release();
  assert.deepEqual(readdirSync(directory), []);
});

test("the socket is private to the owner", POSIX_ONLY, async () => {
  const directory = scratch();
  const lock = await singleInstance(directory);
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
  const child = await holder(directory);
  child.kill("SIGKILL");
  await exited(child);
  // A killed process cannot clean up: its socket file is still there.
  assert.ok(existsSync(join(directory, "lock.sock")));
  const lock = await singleInstance(directory);
  await lock.release();
});

test(
  "a recovery claim left by a dead process does not block the next start",
  POSIX_ONLY,
  async () => {
    const directory = scratch();
    const child = await holder(directory);
    child.kill("SIGKILL");
    await exited(child);
    // A starter that died while it was replacing the stale socket leaves its claim behind.
    const dead = spawn(process.execPath, ["-e", ""]);
    await exited(dead);
    writeFileSync(join(directory, "lock.sock.claim"), String(dead.pid));
    const lock = await singleInstance(directory);
    await lock.release();
    assert.deepEqual(readdirSync(directory), []);
  },
);

test("a recovery claim held by a live process refuses the start", POSIX_ONLY, async () => {
  const directory = scratch();
  const child = await holder(directory);
  child.kill("SIGKILL");
  await exited(child);
  writeFileSync(join(directory, "lock.sock.claim"), String(process.pid));
  await assert.rejects(singleInstance(directory), AlreadyRunning);
});

test("two starts racing for a stale socket leave exactly one holder", POSIX_ONLY, async () => {
  const directory = scratch();
  const child = await holder(directory);
  child.kill("SIGKILL");
  await exited(child);
  const results = await Promise.allSettled([singleInstance(directory), singleInstance(directory)]);
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

test(
  "a path over the system's socket limit is refused with the limit named",
  POSIX_ONLY,
  async () => {
    const directory = join(scratch(), "x".repeat(150));
    mkdirSync(directory);
    await assert.rejects(
      singleInstance(directory),
      (error: unknown) =>
        error instanceof Error &&
        !(error instanceof AlreadyRunning) &&
        /bytes.*limit/.test(error.message),
    );
  },
);
