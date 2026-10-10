/**
 * Claude Code's folder trust, read before a session starts in a bound directory.
 *
 * An SDK session never shows the trust dialog, and Claude Code uses a repository's own hooks, `env`
 * block and helper commands there whether the folder was trusted or not (permissions reference,
 * "What runs before you trust a folder", read 2026-10-08). The daemon therefore starts Claude Code
 * only where the owner has already trusted the folder in the terminal, by the rules Claude Code
 * documents:
 *
 * - in a git repository, the trust is keyed on the repository root (the main checkout's root for
 *   a worktree) and a trusted parent does not cover it;
 * - outside a repository, a trusted folder covers its subdirectories.
 *
 * The record is `projects["<path>"].hasTrustDialogAccepted` in `~/.claude.json`.
 *
 * Claude Code holds one kind of content to a stricter rule than this gate: the `permissions.allow`
 * rules and `additionalDirectories` of a folder's `.claude/settings.json` need that folder's own
 * record, and a trusted parent does not stand for it (same table). In a folder that only a parent's
 * trust covers, a session starts and those rules are left out, with `this workspace has not been
 * trusted` on stderr (measured 2026-10-03, CLI 2.1.286). The terminal shows its dialog again there,
 * listing them, whatever the session did. No SDK or `claude -p` run wrote or changed a record's
 * `hasTrustDialogAccepted` in those measurements; an interactive start that is not trusted writes
 * `false`.
 *
 * The daemon's own git (the footer, `!status`, `!open`) has a second way in, beside a trusted
 * repository key: a repository inside the folder the session started in (`trustedRepository`).
 *
 * Which repository a folder belongs to is read from the filesystem and never asked of git there:
 * git would answer from the folder's own `.git` file, `commondir` and `core.worktree`, which
 * whoever supplied the folder wrote. The layout read here is the one gitrepository-layout(5) and
 * git-worktree(1) document, and the bare-repository test follows `is_git_directory` in git's
 * setup.c (git 2.54.0, read 2026-10-04). The three small files it reads (a `.git` file, a
 * `commondir`, a worktree's `gitdir`) are cut at their line ends as git cuts them (setup.c and
 * worktree.c, git 2.54.0, read 2026-10-06): a different cut names a different path than git's.
 *
 * Every check runs on `node:fs/promises`, so a slow disk never holds the event loop. The Python
 * module ran the same synchronous code on a thread; the awaits here are at the same places, and
 * the paths are compared as directories on disk by device and inode (`stat` with `bigint`, since a
 * 64-bit inode does not fit a number), never by string.
 */
import { type BigIntStats, constants, type Stats } from "node:fs";
import { lstat, open, readFile, readlink, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, resolve } from "node:path";
import { getLogger } from "../../log.ts";
import type { Repository } from "../seam.ts";

export const logger = getLogger("awaydesk.agent.claude.trust");

/** git's own limit for a `.git` file (setup.c, read_gitfile_gently). */
export const GITFILE_BYTES = 1 << 20;

/**
 * Python's `realpath` gave up on a long enough chain of symlinks with a `RecursionError`
 * (3.12); here a nested resolution deeper than this does. The kernel follows 40 links at most.
 */
const MAX_SYMLINK_DEPTH = 255;

/**
 * git would find a repository at the folder that no path the owner trusted can stand for: a bare
 * layout, or metadata that cannot be read.
 */
export class Unkeyed extends Error {
  constructor(cause?: unknown) {
    super("no path the owner trusted can stand for this repository", { cause });
    this.name = "Unkeyed";
  }
}

class SymlinkChainTooDeep extends Error {
  constructor() {
    super("too many nested symbolic links");
    this.name = "SymlinkChainTooDeep";
  }
}

// Python's `OSError` is an error with a `syscall`; its `ValueError` for a NUL in a path is
// Node's `ERR_INVALID_ARG_VALUE`.
function isOsError(error: unknown): boolean {
  return error instanceof Error && typeof (error as { syscall?: unknown }).syscall === "string";
}

function isValueError(error: unknown): boolean {
  return error instanceof Error && (error as { code?: unknown }).code === "ERR_INVALID_ARG_VALUE";
}

/**
 * What a path somebody else wrote can do to a lookup: a system error, a NUL in it, or a chain of
 * symlinks `realpath` gives up on. An exported function answers closed on any of them.
 */
function isFilesystemOddity(error: unknown): boolean {
  return isOsError(error) || isValueError(error) || error instanceof SymlinkChainTooDeep;
}

function isMissing(error: unknown): boolean {
  const code = (error as { code?: unknown } | null)?.code;
  return code === "ENOENT" || code === "ENOTDIR";
}

/** The name a log line gives a failure: the errno code of a system error, else the error's name. */
function errorName(error: unknown): string {
  if (error instanceof Error) {
    const code = (error as { code?: unknown }).code;
    return typeof code === "string" ? code : error.name;
  }
  return typeof error;
}

// Python kept the bytes of a path it read apart (surrogate escapes), so `m\xff` never named a folder
// called `m\ufffd`; a replacement character would. A path read from a file is decoded strictly, and
// one that is not UTF-8 is metadata this module cannot use.
const FATAL_UTF8 = new TextDecoder("utf-8", { fatal: true });

function decodePath(bytes: Buffer): string | null {
  try {
    return FATAL_UTF8.decode(bytes);
  } catch {
    return null;
  }
}

// Python read the record as strict UTF-8 and kept a BOM in the text, where `json.loads` refuses it.
const STRICT_UTF8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

function sameStat(a: BigIntStats, b: BigIntStats): boolean {
  return a.dev === b.dev && a.ino === b.ino;
}

/** `base / name` as pathlib joins it: an absolute name replaces the base, `.` parts go, `..` stays. */
function joinPath(base: string, name: string): string {
  const joined = isAbsolute(name) ? name : `${base}/${name}`;
  const parts = joined.split("/").filter((part) => part !== "" && part !== ".");
  return (joined.startsWith("/") ? "/" : "") + parts.join("/");
}

/** `posixpath.join` for a name that is not absolute. */
function joinSlash(path: string, name: string): string {
  return path.endsWith("/") ? path + name : `${path}/${name}`;
}

/** The folder and every folder above it, nearest first. */
function withParents(folder: string): string[] {
  const chain = [folder];
  for (let up = dirname(folder); up !== chain[chain.length - 1]; up = dirname(up)) {
    chain.push(up);
  }
  return chain;
}

async function joinRealpath(
  start: string,
  target: string,
  seen: Map<string, string | null>,
  depth: number,
): Promise<[string, boolean]> {
  if (depth > MAX_SYMLINK_DEPTH) {
    throw new SymlinkChainTooDeep();
  }
  let path = start;
  let rest = target;
  if (rest.startsWith("/")) {
    rest = rest.slice(1);
    path = "/";
  }
  while (rest) {
    const cut = rest.indexOf("/");
    const name = cut === -1 ? rest : rest.slice(0, cut);
    rest = cut === -1 ? "" : rest.slice(cut + 1);
    if (name === "" || name === ".") {
      continue;
    }
    if (name === "..") {
      path = dirname(path);
      continue;
    }
    const newPath = joinSlash(path, name);
    let isLink = false;
    try {
      isLink = (await lstat(newPath)).isSymbolicLink();
    } catch (error) {
      // Not strict: a component that cannot be looked at is kept as it is.
      if (!isOsError(error)) {
        throw error;
      }
    }
    if (!isLink) {
      path = newPath;
      continue;
    }
    if (seen.has(newPath)) {
      const cached = seen.get(newPath);
      if (cached !== undefined && cached !== null) {
        path = cached;
        continue;
      }
      // The symlink is not resolved, so there is a loop: the part resolved so far, the rest as is.
      return [joinSlash(newPath, rest), false];
    }
    seen.set(newPath, null);
    const [resolved, ok] = await joinRealpath(path, await readlink(newPath), seen, depth + 1);
    path = resolved;
    if (!ok) {
      return [joinSlash(path, rest), false];
    }
    seen.set(newPath, path);
  }
  return [path, true];
}

/**
 * `os.path.realpath` as Python 3.12 does it, not `fs.realpath`: symlinks are resolved and a tail
 * that does not exist is kept, where `fs.realpath` throws on a missing path.
 */
async function realpath(filename: string): Promise<string> {
  // The path goes to the walk as it is, never through `resolve`: that would fold a `..` into the
  // component before it, and the kernel, git and Python's `_joinrealpath` follow the symlink first.
  const absolute = isAbsolute(filename) ? filename : `${process.cwd()}/${filename}`;
  const [path] = await joinRealpath("/", absolute, new Map(), 0);
  // Python's `abspath` at the end: lexical, as `resolve` is.
  return resolve(path);
}

/** `path`'s own entry, a symlink not followed; null when nothing is there. */
async function entryStat(path: string): Promise<Stats | null> {
  try {
    return await lstat(path);
  } catch (error) {
    if (isMissing(error)) {
      return null;
    }
    throw new Unkeyed(error);
  }
}

async function lexists(path: string): Promise<boolean> {
  try {
    await lstat(path);
    return true;
  } catch {
    return false;
  }
}

/** A regular file's content. Never waits on a FIFO, never follows a symlink. */
async function regularFile(path: string): Promise<Buffer | null> {
  let handle: Awaited<ReturnType<typeof open>>;
  try {
    // `O_NOFOLLOW` and `O_NONBLOCK` do not exist on Windows, where the daemon is not supported.
    handle = await open(
      path,
      constants.O_RDONLY | (constants.O_NONBLOCK ?? 0) | (constants.O_NOFOLLOW ?? 0),
    );
  } catch {
    return null;
  }
  try {
    const found = await handle.stat();
    if (!found.isFile() || found.size > GITFILE_BYTES) {
      return null;
    }
    // Up to the limit, not up to `found.size`: a file that grew since the `fstat` is read whole, as
    // Python's `os.read(fd, GITFILE_BYTES)` did, where a prefix would name another path.
    const buffer = Buffer.allocUnsafe(GITFILE_BYTES);
    const { bytesRead } = await handle.read(buffer, 0, GITFILE_BYTES, 0);
    return buffer.subarray(0, bytesRead);
  } catch {
    return null;
  } finally {
    await handle.close();
  }
}

/** `bytes` without the trailing bytes that are any of `strip`, as `bytes.rstrip(strip)`. */
function stripEnd(bytes: Buffer, strip: readonly number[]): Buffer {
  let end = bytes.length;
  while (end > 0 && strip.includes(bytes[end - 1] as number)) {
    end -= 1;
  }
  return bytes.subarray(0, end);
}

const CR = 0x0d;
const LF = 0x0a;
const SPACE = 0x20;
const TAB = 0x09;

/**
 * Whether git could take `folder` for a git dir: it wants a HEAD, and `objects` and `refs` there
 * or in the directory a `commondir` names. Every folder git accepts passes here; a test holds
 * that against the git installed.
 */
async function gitDirLike(folder: string): Promise<boolean> {
  if (!(await lexists(`${folder}/HEAD`))) {
    return false;
  }
  if (await lexists(`${folder}/commondir`)) {
    return true;
  }
  return (await lexists(`${folder}/objects`)) && (await lexists(`${folder}/refs`));
}

/** The git dir a `.git` file names, read as git reads it. */
async function gitfileTarget(gitfile: string): Promise<string | null> {
  const content = await regularFile(gitfile);
  if (content === null || !content.subarray(0, 8).equals(Buffer.from("gitdir: "))) {
    return null;
  }
  // git strips line ends only: a trailing space belongs to the path.
  const target = decodePath(stripEnd(content.subarray(8), [CR, LF]));
  return target ? realpath(joinPath(dirname(gitfile), target)) : null;
}

/**
 * The main checkout that registers `root` as a linked worktree, null when none does.
 *
 * The link is read on the main checkout's side, in `<main>/.git/worktrees/<id>/gitdir`, which
 * git writes there; what `root`'s own `.git` file says only tells where to look.
 */
async function mainCheckout(gitDir: string, root: string): Promise<string | null> {
  const registry = dirname(gitDir);
  const dotGit = dirname(registry);
  if (basename(registry) !== "worktrees" || basename(dotGit) !== ".git") {
    return null;
  }
  // A main checkout is a repository by the walk's own test: a `.git` holding a worktree
  // registry and nothing else, left in a trusted folder outside git, makes it none.
  if (!(await gitDirLike(dotGit))) {
    return null;
  }
  const content = await regularFile(`${gitDir}/gitdir`);
  if (content === null) {
    return null;
  }
  // An absolute path, or one relative to this directory (`git worktree add --relative-paths`).
  // git strips trailing whitespace here (`strbuf_rtrim`, worktree.c get_linked_worktree), and
  // whitespace is those four bytes for it (ctype.c, `sane_ctype`).
  const name = decodePath(stripEnd(content, [SPACE, TAB, CR, LF]));
  if (name === null) {
    return null;
  }
  const registered = joinPath(gitDir, name);
  // The two folders are compared as directories on disk: the same one whatever the case or
  // the Unicode form git wrote, another one for a name one space longer. Not the `.git` files:
  // a file can be hard-linked into a second folder.
  try {
    if (
      basename(registered) !== ".git" ||
      !sameStat(
        await stat(dirname(registered), { bigint: true }),
        await stat(root, { bigint: true }),
      )
    ) {
      return null;
    }
  } catch (error) {
    if (isOsError(error)) {
      return null;
    }
    throw error;
  }
  return dirname(dotGit);
}

/**
 * The repository holding `directory`, null outside git. Rejects with `Unkeyed`.
 *
 * The walk is git's own, with one difference: a folder holding a `.git` entry is keyed on
 * itself, whatever that entry claims, unless a main checkout registers it as its worktree.
 */
export async function locate(directory: string): Promise<Repository | null> {
  try {
    return await locateFolder(await realpath(directory));
  } catch (error) {
    // What a `.git` file or symlink names is somebody else's text: a NUL in it is no path, and
    // `realpath` gives up on a long enough chain of symlinks.
    if (isFilesystemOddity(error)) {
      throw new Unkeyed(error);
    }
    throw error;
  }
}

async function locateFolder(folder: string): Promise<Repository | null> {
  if (!(await stat(folder)).isDirectory()) {
    throw new Unkeyed();
  }
  const passed: string[] = [];
  let gitDirPassed: string | null = null; // the first on the way up, where git would stop
  let found: { root: string; entry: string; mode: Stats } | null = null;
  for (const root of withParents(folder)) {
    const entry = `${root === "/" ? "" : root}/.git`;
    const mode = await entryStat(entry);
    // git walks past a `.git` directory that is no git dir.
    if (mode !== null && !(mode.isDirectory() && !(await gitDirLike(entry)))) {
      found = { root, entry, mode };
      break;
    }
    if (gitDirPassed === null && (await gitDirLike(root))) {
      gitDirPassed = root;
    }
    passed.push(root);
  }
  if (found === null) {
    if (gitDirPassed !== null) {
      throw new Unkeyed();
    }
    return null;
  }
  const { root, entry, mode } = found;
  if (gitDirPassed !== null) {
    // In the root's own `.git` directory, reached in whatever case, the repository is the
    // root's. Anywhere else it is a bare layout below the root: git stops there and reads
    // that folder's own config.
    const own = await stat(entry, { bigint: true });
    let reached = false;
    if (mode.isDirectory()) {
      for (const path of passed) {
        if (sameStat(own, await stat(path, { bigint: true }))) {
          reached = true;
          break;
        }
      }
    }
    if (!reached) {
      throw new Unkeyed();
    }
    return { root, key: root, gitDir: gitDirPassed, insideGitDir: true };
  }
  if (mode.isDirectory()) {
    return { root, key: root, gitDir: entry, insideGitDir: false };
  }
  if (mode.isSymbolicLink()) {
    return { root, key: root, gitDir: await realpath(entry), insideGitDir: false };
  }
  const gitDir = mode.isFile() ? await gitfileTarget(entry) : null;
  const main = gitDir ? await mainCheckout(gitDir, root) : null;
  return { root, key: main ?? root, gitDir, insideGitDir: false };
}

// A `!bind` list checks many folders in a row: the record, several MB, is parsed again only once
// it has changed on disk. The promise is kept, not the result, so the checks of one list that
// arrive while the first is still reading wait for it instead of parsing the record each.
let reading: { key: string; trusted: Promise<ReadonlySet<string>> } | null = null;

async function trustedPaths(home: string): Promise<ReadonlySet<string>> {
  const record = `${home}/.claude.json`;
  let found: BigIntStats;
  try {
    found = await stat(record, { bigint: true });
  } catch (error) {
    if (!isOsError(error)) {
      throw error;
    }
    logger.warning(`could not read Claude Code's trusted folders: ${errorName(error)}`);
    return new Set();
  }
  const key = `${record}\0${found.mtimeNs}\0${found.size}`;
  if (reading === null || reading.key !== key) {
    reading = { key, trusted: readTrusted(record) };
  }
  return reading.trusted;
}

async function readTrusted(recordPath: string): Promise<ReadonlySet<string>> {
  let record: unknown;
  try {
    record = JSON.parse(STRICT_UTF8.decode(await readFile(recordPath)));
  } catch (error) {
    logger.warning(`could not read Claude Code's trusted folders: ${errorName(error)}`);
    return new Set();
  }
  const projects =
    typeof record === "object" && record !== null && !Array.isArray(record)
      ? (record as { projects?: unknown }).projects
      : undefined;
  if (typeof projects !== "object" || projects === null || Array.isArray(projects)) {
    return new Set();
  }
  const accepted = new Set<string>();
  for (const [path, state] of Object.entries(projects)) {
    // A lone surrogate cannot be encoded as a path: Python's lookup of it failed, where Node's
    // would name the folder with a replacement character. With the `u` flag a pair is one code
    // point, so `\p{Surrogate}` finds only a lone one (`isWellFormed` is not in the project's lib).
    if (
      !LONE_SURROGATE.test(path) &&
      typeof state === "object" &&
      state !== null &&
      !Array.isArray(state) &&
      (state as { hasTrustDialogAccepted?: unknown }).hasTrustDialogAccepted === true
    ) {
      accepted.add(path);
    }
  }
  return accepted;
}

const LONE_SURROGATE = /\p{Surrogate}/u;

const RECORDED_BATCH = 64;

/**
 * Whether the recorded path is the folder `mine` and spells it: a recorded path stands for the
 * folder it spells, not for one a symlink in it leads to, so none of its components may be a
 * symlink.
 */
async function spellsFolder(recorded: string, mine: BigIntStats): Promise<boolean> {
  try {
    return (
      sameStat(mine, await lstat(recorded, { bigint: true })) &&
      (await realpath(recorded)) === recorded
    );
  } catch (error) {
    if (isFilesystemOddity(error)) {
      return false;
    }
    throw error;
  }
}

/**
 * Whether `path` is a folder the owner trusted: the same directory on disk as a recorded path,
 * so a path in another case or Unicode form is that folder and `app ` is not `app`.
 */
async function isTrusted(path: string, home: string): Promise<boolean> {
  const trusted = await trustedPaths(home);
  if (trusted.has(path)) {
    return true;
  }
  let mine: BigIntStats;
  try {
    mine = await stat(path, { bigint: true });
  } catch (error) {
    if (isOsError(error)) {
      return false;
    }
    throw error;
  }
  // The recorded paths are looked at RECORDED_BATCH at a time: one lstat after the other would
  // cost a round trip to the thread pool each. The verdicts are read in the record's order, so a
  // failure past the first match is not raised, as in a loop that stops there.
  const recorded = [...trusted];
  for (let from = 0; from < recorded.length; from += RECORDED_BATCH) {
    const batch = recorded.slice(from, from + RECORDED_BATCH);
    for (const verdict of await Promise.allSettled(batch.map((one) => spellsFolder(one, mine)))) {
      if (verdict.status === "rejected") {
        throw verdict.reason;
      }
      if (verdict.value) {
        return true;
      }
    }
  }
  return false;
}

/**
 * Whether `path` lies strictly inside `folder`, both as they are on disk: a symlink in the
 * folder that leads elsewhere is not inside it.
 */
async function isInside(path: string, folder: string): Promise<boolean> {
  let real: string;
  let resolved: string;
  try {
    real = await realpath(folder);
    resolved = await realpath(path);
  } catch (error) {
    if (isFilesystemOddity(error)) {
      return false;
    }
    throw error;
  }
  return resolved !== real && resolved.startsWith(real.endsWith("/") ? real : `${real}/`);
}

/**
 * The common git dir of `gitDir`: the directory its `commondir` file names, as git reads it
 * (relative to `gitDir`), else `gitDir` itself. Null when the file cannot be read as git would,
 * so the answer is never a guess: git dies on an empty `commondir`, and it strips every trailing
 * CR and LF from a name (`get_common_dir_noenv`, setup.c, v2.54.0).
 */
async function commonDir(gitDir: string): Promise<string | null> {
  if (!(await lexists(`${gitDir}/commondir`))) {
    return gitDir;
  }
  const content = await regularFile(`${gitDir}/commondir`);
  if (content === null || content.length === 0) {
    return null;
  }
  const name = decodePath(stripEnd(content, [CR, LF]));
  if (name === null) {
    return null;
  }
  try {
    return await realpath(joinPath(gitDir, name));
  } catch (error) {
    if (isFilesystemOddity(error)) {
      return null;
    }
    throw error;
  }
}

/**
 * Whether everything git would read for `repository` lies strictly inside `sessionFolder`: its
 * key, its git dir and its common dir (a worktree's config and hooks live in the main checkout's).
 * A `.git` that leads out of the folder makes git read someone else's config.
 */
async function heldBy(repository: Repository, sessionFolder: string): Promise<boolean> {
  const gitDir = repository.gitDir;
  if (gitDir === null) {
    return false;
  }
  const common = await commonDir(gitDir);
  if (common === null) {
    return false;
  }
  for (const path of [repository.key, gitDir, common]) {
    if (!(await isInside(path, sessionFolder))) {
      return false;
    }
  }
  return true;
}

/**
 * The repository holding `directory` when the daemon's own git may run in it; null outside git,
 * where the layout has no key, and in a repository neither of these covers:
 *
 * - its key is a path the owner trusted in Claude Code (a worktree's key is its main checkout);
 * - its key, its git dir and its common dir all lie strictly inside `sessionFolder`, the folder
 *   the session was started in, and that folder passes `workspaceTrusted`.
 *
 * The second is the owner's decision (2026-10-05): Slack is only an interface to Claude Code,
 * which, launched in a trusted folder, works in its subfolders. It reaches no further: a
 * repository outside `sessionFolder` needs its own trust since the agent may `cd` anywhere, and
 * `workspaceTrusted`, which gates a session's start and `!bind`, is not widened. Inside is
 * decided on resolved paths, so a symlink in the folder that leads to a repository elsewhere, and
 * a worktree whose main checkout is elsewhere, are not inside. The git dir and the common dir are
 * held to it too, since a `.git` file, a `.git` symlink, a moved worktree or a `commondir` inside
 * the folder can name a repository outside it, whose config git would then read. Nothing here
 * runs git.
 */
export async function trustedRepository(
  directory: string,
  sessionFolder: string,
  home: string = homedir(),
): Promise<Repository | null> {
  let repository: Repository | null;
  try {
    repository = await locate(directory);
  } catch (error) {
    if (error instanceof Unkeyed) {
      return null;
    }
    throw error;
  }
  if (repository === null) {
    return null;
  }
  if (await isTrusted(repository.key, home)) {
    return repository;
  }
  // The second way in, for a repository the session's folder holds.
  if ((await heldBy(repository, sessionFolder)) && (await workspaceTrusted(sessionFolder, home))) {
    return repository;
  }
  return null;
}

/**
 * Whether the owner trusted `directory` in Claude Code, as the terminal would decide; false where
 * git would find a repository that no trusted path stands for.
 */
export async function workspaceTrusted(
  directory: string,
  home: string = homedir(),
): Promise<boolean> {
  let repository: Repository | null;
  try {
    repository = await locate(directory);
  } catch (error) {
    if (error instanceof Unkeyed) {
      return false;
    }
    throw error;
  }
  if (repository !== null) {
    return isTrusted(repository.key, home);
  }
  let folder: string;
  try {
    folder = await realpath(directory);
  } catch (error) {
    if (isFilesystemOddity(error)) {
      return false;
    }
    throw error;
  }
  for (const path of withParents(folder)) {
    if (await isTrusted(path, home)) {
      return true;
    }
  }
  return false;
}
