/**
 * One daemon at a time: Socket Mode spreads events across every open connection.
 *
 * What holds, by platform (`singleInstance` picks the mechanism):
 *
 * - macOS, the only supported host: the lock is the open file description of the configuration
 *   directory itself, opened with `O_EXLOCK | O_NONBLOCK`. That is `flock(LOCK_EX | LOCK_NB)` on
 *   the directory, exactly the lock the Python daemon takes, so a Python daemon and this one
 *   refuse each other. It adds no file, and the kernel drops it when the process dies, a killed
 *   one included, and whatever child processes outlive it (the descriptor is close-on-exec).
 *   Renaming or removing `lock.sock` or any other file in the directory changes nothing.
 * - Linux: a listening Unix socket `lock.sock` in the directory, with `lock.sock.claim` taken
 *   around every start so that the recovery of a stale socket is never raced. Linux has no
 *   `O_EXLOCK`, and Node has no `flock`. This lock does not exclude the Python daemon, which
 *   holds a `flock` on the directory, and a path-named lock has one more limit: removing
 *   `lock.sock` under a live holder lets a second start in, since the new socket is a different
 *   file. A claim file left behind blocks a start for at most `CLAIM_STALE_MS`.
 * - Windows: a named pipe derived from the directory's real path. The system drops it with its
 *   process and refuses a second one of the same name; no claim file is involved. It does not
 *   exclude the Python daemon either.
 */
import { createHash } from "node:crypto";
import {
  chmodSync,
  closeSync,
  constants,
  lstatSync,
  openSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import net from "node:net";
import { join } from "node:path";

// `O_EXLOCK` of macOS's `open(2)`: "atomically obtain an exclusive lock", with flock(2) semantics;
// `#define O_EXLOCK 0x00000020` in `sys/fcntl.h` of the MacOSX SDK (`man 2 open` and that header,
// read on 2026-10-10). Node's `fs.constants` does not export it and Linux has no such flag; Node
// hands the number to `open(2)` through libuv. Measured on macOS the same day, on Node 22 and 26:
// it conflicts with Python's `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on the directory in both
// directions, failing with `EAGAIN` (macOS's `EWOULDBLOCK`, which `open(2)` documents).
const O_EXLOCK = 0x20;

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
export type ListenFn = (address: string) => Promise<net.Server>;

export const listenLocal: ListenFn = (address) =>
  new Promise((resolve, reject) => {
    // The lock accepts no data: whoever connects is told nothing and dropped at once.
    const server = net.createServer((connection) => connection.destroy());
    server.once("error", reject);
    server.listen({ path: address }, () => {
      server.off("error", reject);
      resolve(server);
    });
  });

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

/** A claim older than this by its mtime is dead: a recovery takes milliseconds. */
export const CLAIM_STALE_MS = 5_000;

/**
 * Takes the claim around a start: an exclusive create of `claim`. A claim file older than
 * `CLAIM_STALE_MS` by its mtime is cleared and taken once more, whatever it holds: a starter that
 * died in the milliseconds of its start, or failed to write its pid, leaves one behind. Two
 * starters that both find the same stale claim may both clear it, the second one removing the
 * first's fresh claim; `replaceStale` checks the socket file again for that case.
 */
function takeClaim(claim: string, now: () => number): boolean {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      writeFileSync(claim, String(process.pid), { flag: "wx", mode: 0o600 });
      return true;
    } catch (error) {
      if (errorCode(error) !== "EEXIST") throw error;
    }
    let age: number;
    try {
      age = now() - statSync(claim).mtimeMs;
    } catch (error) {
      if (errorCode(error) === "ENOENT") continue; // released meanwhile: take it
      throw error;
    }
    if (age <= CLAIM_STALE_MS) return false;
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
 * Replaces a socket file whose process is dead by a listener of this one, under the claim. The
 * file is removed only if it is still the inode that was probed and still refuses connections
 * (the removal follows the second look with no await between them): the claim keeps another
 * starter out, and this keeps a holder that started without it, or took a stolen claim, alive.
 * Rejects with `refusal` when the lock is, or is being, taken.
 */
async function replaceStale(
  address: string,
  refusal: AlreadyRunning,
  listenOn: ListenFn,
): Promise<net.Server> {
  const probed = inodeAt(address);
  // A socket file outlives a process killed before it could remove it: a connection that is
  // accepted means the holder is alive, one that is refused means the file is stale.
  if (await accepts(address)) throw refusal;
  if (inodeAt(address) !== probed) throw refusal; // the file was replaced under us
  rmSync(address, { force: true });
  try {
    return await listenOn(address);
  } catch (error) {
    // A starter that found no file listened first: it holds the lock.
    throw nameTaken(error) ? refusal : error;
  }
}

/** What `socketLock` may be given: a test passes its own clock and its own `listen`. */
export interface SocketLockDeps {
  readonly now?: () => number;
  readonly listen?: ListenFn;
}

/**
 * The socket mechanism (Linux, Windows): takes the lock by listening on a local socket in
 * `directory`, so the kernel drops it when the process dies. Rejects with `AlreadyRunning` while
 * another process holds it. Exported so its tests run on macOS too, where `singleInstance`
 * does not use it.
 *
 * The claim is taken before the first `listen`, not only for a recovery: a starter that has bound
 * its socket and not yet called `listen` refuses connections, and a second starter that probed
 * it then would read it as stale and remove it. A start that finds a fresh claim is refused.
 */
export async function socketLock(
  directory: string,
  deps: SocketLockDeps = {},
): Promise<InstanceLock> {
  const { now = Date.now, listen: listenOn = listenLocal } = deps;
  const address = lockAddress(directory);
  const refusal = new AlreadyRunning(`another awaydesk is running (lock on ${directory})`);
  // A named pipe is created whole or refused by the system: no claim is needed there.
  const claim = process.platform === "win32" ? null : `${address}.claim`;
  if (claim !== null && !takeClaim(claim, now)) throw refusal;
  try {
    let server: net.Server;
    try {
      server = await listenOn(address);
    } catch (error) {
      if (!nameTaken(error)) throw error;
      // A named pipe vanishes with its process: one in use is always a live holder.
      if (claim === null) throw refusal;
      server = await replaceStale(address, refusal, listenOn);
    }
    if (claim !== null) {
      // The kernel lets only a user with write access connect to a socket, and a new one takes
      // the umask's mode: close it to everyone else right after listening, without touching the
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
  } finally {
    if (claim !== null) rmSync(claim, { force: true });
  }
}

/**
 * The macOS mechanism: the lock is the open file description of `directory`, opened with
 * `O_EXLOCK | O_NONBLOCK`, which is the `flock` the Python daemon takes on the same directory.
 */
async function directoryLock(directory: string): Promise<InstanceLock> {
  let fd: number;
  try {
    fd = openSync(directory, constants.O_RDONLY | O_EXLOCK | constants.O_NONBLOCK);
  } catch (error) {
    const code = errorCode(error);
    if (code === "EAGAIN" || code === "EWOULDBLOCK") {
      throw new AlreadyRunning(`another awaydesk is running (lock on ${directory})`);
    }
    throw error;
  }
  let open = true;
  return {
    release: async () => {
      // A second close would close whatever descriptor the number was handed to since.
      if (!open) return;
      open = false;
      closeSync(fd);
    },
  };
}

/**
 * Takes the lock on `directory` by the mechanism of this platform (see the header). Rejects with
 * `AlreadyRunning` while another process holds it.
 */
export function singleInstance(directory: string): Promise<InstanceLock> {
  return process.platform === "darwin" ? directoryLock(directory) : socketLock(directory);
}
