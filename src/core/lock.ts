/** One daemon at a time: Socket Mode spreads events across every open connection. */
import { createHash } from "node:crypto";
import { chmodSync, lstatSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import net from "node:net";
import { join } from "node:path";

// A Unix socket path must fit `sun_path`: 104 bytes on macOS and the BSDs, 108 on Linux, one of
// them the terminating NUL.
const SOCKET_PATH_LIMIT =
  process.platform === "darwin" || process.platform.endsWith("bsd") ? 104 : 108;
const SOCKET_NAME = "lock.sock";

/** Another awaydesk process holds the lock. */
export class AlreadyRunning extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AlreadyRunning";
  }
}

/** A held lock; `release` lets the next start in. */
export interface InstanceLock {
  release(): Promise<void>;
}

// Listening on a name that is taken fails with EADDRINUSE; macOS answers EEXIST when two binds
// race for the same name (seen with six starters at once).
function nameTaken(error: unknown): boolean {
  const code = errorCode(error);
  return code === "EADDRINUSE" || code === "EEXIST";
}

function errorCode(error: unknown): string | undefined {
  return error instanceof Error && "code" in error && typeof error.code === "string"
    ? error.code
    : undefined;
}

/** The address the lock listens on: a socket file in `directory`, a named pipe on Windows. */
function lockAddress(directory: string): string {
  if (process.platform === "win32") {
    const real = realpathSync.native(directory).toLowerCase();
    return `\\\\.\\pipe\\awaydesk-${createHash("sha256").update(real).digest("hex")}`;
  }
  const path = join(directory, SOCKET_NAME);
  const bytes = Buffer.byteLength(path);
  if (bytes >= SOCKET_PATH_LIMIT) {
    throw new Error(
      `the lock socket path is ${bytes} bytes, over the ${SOCKET_PATH_LIMIT - 1}-byte limit of ` +
        `Unix sockets on this system; use a shorter configuration directory: ${path}`,
    );
  }
  return path;
}

/** Listens on `address`; resolves to the server, rejects with the listen error (`EADDRINUSE`...). */
function listen(address: string): Promise<net.Server> {
  return new Promise((resolve, reject) => {
    // The lock accepts no data: whoever connects is told nothing and dropped at once.
    const server = net.createServer((connection) => connection.destroy());
    server.once("error", reject);
    server.listen({ path: address }, () => {
      server.off("error", reject);
      resolve(server);
    });
  });
}

/** True when a process accepts connections at `path`, false when nothing is listening there. */
function accepts(path: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    const probe = net.connect({ path });
    // The holder drops the connection at once; a reset after the verdict is not an error.
    probe.on("error", (error) => {
      const code = errorCode(error);
      if (code === "ECONNREFUSED" || code === "ENOENT") resolve(false);
      else reject(error);
    });
    probe.once("connect", () => {
      resolve(true);
      probe.destroy();
    });
  });
}

/** True when a process with this pid exists (one of another user counts as existing). */
function processExists(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return errorCode(error) !== "ESRCH";
  }
}

/**
 * Takes the right to remove a stale socket file: an exclusive create of `claim`, holding this
 * pid. A claim left by a process that no longer exists (it died in the few milliseconds of its
 * recovery) is cleared and taken once more.
 */
function takeClaim(claim: string): boolean {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      writeFileSync(claim, String(process.pid), { flag: "wx", mode: 0o600 });
      return true;
    } catch (error) {
      if (errorCode(error) !== "EEXIST") throw error;
    }
    let owner: number;
    try {
      owner = Number.parseInt(readFileSync(claim, "utf8"), 10);
    } catch (error) {
      if (errorCode(error) === "ENOENT") continue; // released meanwhile: take it
      throw error;
    }
    // An empty file is a claim being written: its owner is alive.
    if (Number.isNaN(owner) || processExists(owner)) return false;
    rmSync(claim, { force: true });
  }
  return false;
}

/** The inode at `path`, or undefined when there is no file. */
function inodeAt(path: string): number | undefined {
  try {
    return lstatSync(path).ino;
  } catch (error) {
    if (errorCode(error) === "ENOENT") return undefined;
    throw error;
  }
}

/**
 * Replaces a socket file whose process is dead by a listener of this one. Two starters that
 * both find the stale file must not both remove it: the second would delete the first's live
 * socket and both would run. So one of them, the holder of an exclusive claim file, removes it
 * and listens while the other is told the lock is taken. Under the claim the file is probed
 * again and removed only if it is still the inode that was probed (the two steps are
 * synchronous calls with no await between them). Rejects with `refusal` when the lock is, or is
 * being, taken.
 */
async function replaceStale(address: string, refusal: AlreadyRunning): Promise<net.Server> {
  const claim = `${address}.claim`;
  if (!takeClaim(claim)) throw refusal;
  try {
    const probed = inodeAt(address);
    if (await accepts(address)) throw refusal; // a holder started while we waited for the claim
    if (inodeAt(address) !== probed) throw refusal; // the file was replaced under us
    rmSync(address, { force: true });
    try {
      return await listen(address);
    } catch (error) {
      // A starter that found no file listened first: it holds the lock.
      throw nameTaken(error) ? refusal : error;
    }
  } finally {
    rmSync(claim, { force: true });
  }
}

/**
 * Takes the lock by listening on a local socket in `directory`, so the kernel drops it when the
 * process dies. Rejects with `AlreadyRunning` while another process holds it.
 */
export async function singleInstance(directory: string): Promise<InstanceLock> {
  const address = lockAddress(directory);
  const refusal = new AlreadyRunning(`another awaydesk is running (lock on ${directory})`);
  let server: net.Server;
  try {
    server = await listen(address);
  } catch (error) {
    if (!nameTaken(error)) throw error;
    // A named pipe vanishes with its process: one in use is always a live holder.
    if (process.platform === "win32") throw refusal;
    // A socket file outlives a process killed before it could remove it: a connection that
    // is accepted means the holder is alive, one that is refused means the file is stale.
    if (await accepts(address)) throw refusal;
    server = await replaceStale(address, refusal);
  }
  if (process.platform !== "win32") {
    // The kernel lets only a user with write access connect to a socket, and a new one takes the
    // umask's mode: close it to everyone else right after listening, without touching the
    // process-wide umask.
    try {
      chmodSync(address, 0o600);
    } catch (error) {
      server.close(); // the file is not ours any more: do not run as a holder nobody can reach
      throw error;
    }
  }
  return {
    // libuv unlinks the socket file when the listener closes.
    release: () =>
      new Promise<void>((resolve, reject) =>
        server.close((error) => (error === undefined ? resolve() : reject(error))),
      ),
  };
}
