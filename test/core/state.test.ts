import assert from "node:assert/strict";
import fs, {
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import {
  channelRecord,
  parseThread,
  StateError,
  StateStore,
  type ThreadKey,
  threadState,
  UnknownChannel,
} from "../../src/core/state.ts";

const POSIX_ONLY = { skip: process.platform === "win32" };

const CHANNEL = "C000CHAN";
const OTHER_CHANNEL = "C000OTHR";
const THREAD_TS = "1790549806.565369";
const OTHER_THREAD_TS = "1790549900.100000";
const SESSION = "4e8c1111-2222-3333-4444-555566667777";

const made: string[] = [];

function scratch(): string {
  const directory = mkdtempSync(join(tmpdir(), "awd-state-"));
  made.push(directory);
  return directory;
}

after(() => {
  for (const directory of made) rmSync(directory, { recursive: true, force: true });
});

/** The file as a parsed object, and the one thread the helpers below look at. */
interface StateFile {
  version: number;
  channels: Record<string, { threads: Record<string, Record<string, unknown>> }>;
}

function read(path: string): StateFile {
  return JSON.parse(readFileSync(path, "utf8")) as StateFile;
}

function written(path: string): Record<string, unknown> {
  const thread = read(path).channels[CHANNEL]?.threads[THREAD_TS];
  assert.ok(thread !== undefined);
  return thread;
}

/** The thread, which the test has just created. */
function mustThread(store: StateStore, channelId = CHANNEL, threadTs = THREAD_TS) {
  const thread = store.thread(channelId, threadTs);
  assert.ok(thread !== null);
  return thread;
}

/** A v2 file holding one thread whose keys are `thread`, in the folder `directory`. */
function writeThreadFile(path: string, directory: string, thread: Record<string, unknown>): void {
  const channel = { directory, notice_pending: false, threads: { [THREAD_TS]: thread } };
  writeFileSync(path, JSON.stringify({ version: 2, channels: { [CHANNEL]: channel } }));
}

test("a new store is empty", () => {
  assert.equal(new StateStore(join(scratch(), "state.json")).channel(CHANNEL), null);
});

test("bind creates a channel with no threads", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  assert.deepEqual(store.channel(CHANNEL), channelRecord(join(dir, "project")));
});

test("open thread stores the channels current folder", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  const thread = store.openThread(CHANNEL, THREAD_TS);
  assert.deepEqual(thread, threadState(join(dir, "project")));
  assert.deepEqual(store.thread(CHANNEL, THREAD_TS), thread);
});

test("open thread on an unbound channel raises", () => {
  const store = new StateStore(join(scratch(), "state.json"));
  assert.throws(() => store.openThread(CHANNEL, THREAD_TS), UnknownChannel);
});

test("open thread is idempotent", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  const first = store.openThread(CHANNEL, THREAD_TS, "ignored-on-repeat");
  const second = store.openThread(CHANNEL, THREAD_TS, "also-ignored");
  assert.deepEqual(first, second);
  assert.equal(first.sessionId, "ignored-on-repeat");
});

test("a thread keeps its folder across a rebind", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "a"));
  store.openThread(CHANNEL, THREAD_TS);
  store.bind(CHANNEL, join(dir, "b"));
  assert.deepEqual(
    store.channel(CHANNEL),
    channelRecord(join(dir, "b"), {
      threads: new Map([[THREAD_TS, threadState(join(dir, "a"))]]),
    }),
  );
  assert.deepEqual(store.thread(CHANNEL, THREAD_TS), threadState(join(dir, "a")));
});

test("set session bypass and effort round trip", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.setSession(CHANNEL, THREAD_TS, SESSION);
  store.setBypass(CHANNEL, THREAD_TS, true);
  store.setEffort(CHANNEL, THREAD_TS, "high");

  const reloaded = new StateStore(path);
  assert.deepEqual(
    reloaded.thread(CHANNEL, THREAD_TS),
    threadState(join(dir, "project"), { sessionId: SESSION, bypass: true, effort: "high" }),
  );
});

test("set session bypass and effort are a no op for an unknown thread", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, dir);
  store.setSession(CHANNEL, THREAD_TS, SESSION);
  store.setBypass(CHANNEL, THREAD_TS, true);
  store.setEffort(CHANNEL, THREAD_TS, "high");
  assert.equal(store.thread(CHANNEL, THREAD_TS), null);
});

test("set session does not rewrite when unchanged", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, dir);
  store.openThread(CHANNEL, THREAD_TS);
  store.setSession(CHANNEL, THREAD_TS, SESSION);
  const before = statSync(path, { bigint: true });
  store.setSession(CHANNEL, THREAD_TS, SESSION);
  const after = statSync(path, { bigint: true });
  assert.equal(after.mtimeNs, before.mtimeNs);
  // A rewrite renames a new file over the old one, so it would also change the inode.
  assert.equal(after.ino, before.ino);
});

test("remove thread", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, dir);
  store.openThread(CHANNEL, THREAD_TS);
  store.removeThread(CHANNEL, THREAD_TS);
  assert.equal(store.thread(CHANNEL, THREAD_TS), null);
  // Removing again, or removing from an unbound channel, is a no-op.
  store.removeThread(CHANNEL, THREAD_TS);
  store.removeThread(OTHER_CHANNEL, THREAD_TS);
});

test("holder finds a session across channels", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "a"));
  store.bind(OTHER_CHANNEL, join(dir, "b"));
  store.openThread(CHANNEL, THREAD_TS);
  store.openThread(OTHER_CHANNEL, OTHER_THREAD_TS);
  store.setSession(OTHER_CHANNEL, OTHER_THREAD_TS, SESSION);
  assert.deepEqual(store.holder(SESSION), [OTHER_CHANNEL, OTHER_THREAD_TS]);
  assert.equal(store.holder("no-such-session"), null);
});

for (const [label, value] of [
  ['"true"', "true"],
  ['"false"', "false"],
  ["1", 1],
  ["null", null],
  ["false", false],
] as const) {
  test(`only a literal true turns bypass on and the rest is unset [${label}]`, () => {
    const dir = scratch();
    const path = join(dir, "state.json");
    writeThreadFile(path, dir, { directory: dir, session_id: null, bypass: value, effort: null });
    assert.deepEqual(
      new StateStore(path).thread(CHANNEL, THREAD_TS),
      threadState(dir, { bypass: null }),
    );
  });
}

test("v2 round trips every field", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.setSession(CHANNEL, THREAD_TS, SESSION);
  store.setBypass(CHANNEL, THREAD_TS, true);
  store.setEffort(CHANNEL, THREAD_TS, "low");

  assert.deepEqual(read(path), {
    version: 2,
    channels: {
      [CHANNEL]: {
        directory: join(dir, "project"),
        notice_pending: false,
        threads: {
          [THREAD_TS]: {
            directory: join(dir, "project"),
            session_id: SESSION,
            bypass: true,
            effort: "low",
            open_replies: [],
            requests: [],
            status: null,
            ended: null,
          },
        },
      },
    },
  });
});

test("a v1 file migrates folder kept session and bypass dropped", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const v1 = {
    version: 1,
    channels: {
      [CHANNEL]: { directory: join(dir, "project"), session_id: "old", bypass: true },
    },
  };
  writeFileSync(path, JSON.stringify(v1));

  const store = new StateStore(path);
  assert.deepEqual(
    store.channel(CHANNEL),
    channelRecord(join(dir, "project"), { noticePending: true }),
  );
  assert.deepEqual(store.pendingNotices(), [CHANNEL]);

  // The migration is written back at once, as v2.
  assert.deepEqual(read(path), {
    version: 2,
    channels: {
      [CHANNEL]: { directory: join(dir, "project"), notice_pending: true, threads: {} },
    },
  });
});

test("clear notice", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  writeFileSync(
    path,
    JSON.stringify({
      version: 1,
      channels: { [CHANNEL]: { directory: dir, session_id: null } },
    }),
  );
  const store = new StateStore(path);
  assert.deepEqual(store.pendingNotices(), [CHANNEL]);
  store.clearNotice(CHANNEL);
  assert.deepEqual(store.pendingNotices(), []);
  // A no-op for an unbound channel or one already clear.
  store.clearNotice(OTHER_CHANNEL);
  store.clearNotice(CHANNEL);
});

test("an unknown version is refused", () => {
  const path = join(scratch(), "state.json");
  writeFileSync(path, JSON.stringify({ version: 3, channels: {} }));
  assert.throws(
    () => new StateStore(path),
    (error: unknown) => error instanceof StateError && /state\.json/.test(error.message),
  );
});

test("a corrupt file is refused not discarded", () => {
  const path = join(scratch(), "state.json");
  writeFileSync(path, "{not json");
  assert.throws(
    () => new StateStore(path),
    (error: unknown) => error instanceof StateError && /state\.json/.test(error.message),
  );
  assert.equal(readFileSync(path, "utf8"), "{not json");
});

test("a missing file is empty", () => {
  assert.equal(new StateStore(join(scratch(), "state.json")).channel(CHANNEL), null);
});

test("file is private and nothing else is left behind", POSIX_ONLY, () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, dir);
  assert.deepEqual(readdirSync(dir).sort(), ["state.json"]);
  assert.equal(statSync(join(dir, "state.json")).mode & 0o777, 0o600);
});

test("a failed write keeps the previous file", (t) => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "a"));
  const before = readFileSync(path, "utf8");

  // The rename is the step that makes a write take effect: failing it is Python's `os.replace`
  // monkeypatch.
  t.mock.method(fs, "renameSync", () => {
    throw new Error("disk full");
  });
  assert.throws(() => store.bind(CHANNEL, join(dir, "b")), /disk full/);
  assert.equal(readFileSync(path, "utf8"), before);
  assert.deepEqual(readdirSync(dir).sort(), ["state.json"]);
});

// TestPrune: `prune` calls `alive` once per distinct folder and reports how many entries it removed.

test("removes a thread whose session is gone", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.setSession(CHANNEL, THREAD_TS, SESSION);

  const removed = store.prune(() => [], 1790549806.565369);
  assert.equal(removed, 1);
  assert.equal(store.thread(CHANNEL, THREAD_TS), null);
});

test("keeps a thread whose session is alive", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.setSession(CHANNEL, THREAD_TS, SESSION);

  const removed = store.prune(() => [SESSION], 1790549806.565369);
  assert.equal(removed, 0);
  assert.notEqual(store.thread(CHANNEL, THREAD_TS), null);
});

test("removes a no session thread older than a day", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);

  const rootTime = Number(THREAD_TS);
  const removed = store.prune(() => [], rootTime + 86_401);
  assert.equal(removed, 1);
  assert.equal(store.thread(CHANNEL, THREAD_TS), null);
});

test("keeps a young no session thread", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);

  const rootTime = Number(THREAD_TS);
  const removed = store.prune(() => [], rootTime + 3_600);
  assert.equal(removed, 0);
  assert.notEqual(store.thread(CHANNEL, THREAD_TS), null);
});

test("calls alive once per distinct folder", () => {
  const calls: string[] = [];
  const alive = (directory: string): string[] => {
    calls.push(directory);
    return [SESSION];
  };

  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.setSession(CHANNEL, THREAD_TS, SESSION);
  store.openThread(CHANNEL, OTHER_THREAD_TS);
  store.setSession(CHANNEL, OTHER_THREAD_TS, SESSION);

  const removed = store.prune(alive, 1790549806.565369);
  assert.equal(removed, 0);
  assert.deepEqual(calls, [join(dir, "project")]);
});

test("alive raising leaves the file untouched", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.setSession(CHANNEL, THREAD_TS, SESSION);
  const before = readFileSync(path, "utf8");

  const boom = (): string[] => {
    throw new Error("folder is gone");
  };

  assert.throws(() => store.prune(boom, 1790549806.565369), /folder is gone/);
  assert.equal(readFileSync(path, "utf8"), before);
  assert.notEqual(store.thread(CHANNEL, THREAD_TS), null);
});

test("a v2 file with no repair fields loads with them empty", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  writeThreadFile(path, join(dir, "project"), {
    directory: join(dir, "project"),
    session_id: SESSION,
    bypass: false,
    effort: null,
  });
  const thread = new StateStore(path).thread(CHANNEL, THREAD_TS);
  assert.deepEqual(thread, threadState(join(dir, "project"), { sessionId: SESSION }));
  assert.ok(thread !== null);
  assert.deepEqual(thread.openReplies, []);
  assert.deepEqual(thread.requests, []);
  assert.equal(thread.status, null);
});

test("a file with 1304c5e s single open reply field loads as nothing open", () => {
  // That field never shipped past the fix round: a file written by it is read exactly like one
  // written before crash repair existed at all.
  const dir = scratch();
  const path = join(dir, "state.json");
  writeThreadFile(path, join(dir, "project"), {
    directory: join(dir, "project"),
    session_id: null,
    bypass: false,
    effort: null,
    open_reply: "1790000000.000001",
  });
  const thread = new StateStore(path).thread(CHANNEL, THREAD_TS);
  assert.ok(thread !== null);
  assert.deepEqual(thread.openReplies, []);
});

test("repair fields round trip and are ignored by code that predates them", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.replaceOpenReply(CHANNEL, THREAD_TS, null, "1790000000.000001");
  store.addRequest(CHANNEL, THREAD_TS, "1790000000.000002");
  store.addRequest(CHANNEL, THREAD_TS, "1790000000.000003");
  store.setStatusPending(CHANNEL, THREAD_TS, "hourglass_flowing_sand");

  const reloaded = new StateStore(path).thread(CHANNEL, THREAD_TS);
  assert.deepEqual(
    reloaded,
    threadState(join(dir, "project"), {
      openReplies: ["1790000000.000001"],
      requests: ["1790000000.000002", "1790000000.000003"],
      status: "hourglass_flowing_sand",
    }),
  );
  // Code that only knows `parseThread`'s old fields ignores the extra keys.
  const raw = written(path);
  const oldShape = {
    directory: raw.directory,
    session_id: raw.session_id,
    bypass: raw.bypass,
    effort: raw.effort,
  };
  assert.deepEqual(parseThread(oldShape), threadState(join(dir, "project")));
});

test("replace open reply tracks two sinks independently", () => {
  // The bug the fix round measured: two replies open at once (a background task's own reply
  // outliving the turn that started it) must never step on each other's entry.
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, dir);
  store.openThread(CHANNEL, THREAD_TS);
  store.replaceOpenReply(CHANNEL, THREAD_TS, null, "a-1"); // sink A's first message
  store.replaceOpenReply(CHANNEL, THREAD_TS, null, "b-1"); // sink B's first message
  assert.deepEqual(mustThread(store).requests, []); // sanity: unrelated field
  assert.deepEqual([...mustThread(store).openReplies].sort(), ["a-1", "b-1"]);
  store.replaceOpenReply(CHANNEL, THREAD_TS, "a-1", null); // sink A settles; B untouched
  assert.deepEqual(mustThread(store).openReplies, ["b-1"]);
  store.replaceOpenReply(CHANNEL, THREAD_TS, "b-1", "b-2"); // sink B's continuation
  assert.deepEqual(mustThread(store).openReplies, ["b-2"]);
  store.replaceOpenReply(CHANNEL, THREAD_TS, "b-2", null);
  assert.deepEqual(mustThread(store).openReplies, []);
});

test("replace open reply is a no op that changes nothing for an unknown thread", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, dir);
  store.replaceOpenReply(CHANNEL, THREAD_TS, null, "ts-1");
  assert.equal(store.thread(CHANNEL, THREAD_TS), null);
});

test("remove request drops only the named one", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.addRequest(CHANNEL, THREAD_TS, "ts-1");
  store.addRequest(CHANNEL, THREAD_TS, "ts-2");
  store.removeRequest(CHANNEL, THREAD_TS, "ts-1");
  const thread = mustThread(store);
  assert.deepEqual(thread.requests, ["ts-2"]);
  store.removeRequest(CHANNEL, THREAD_TS, "ts-1"); // already gone: a no-op
  assert.deepEqual(store.thread(CHANNEL, THREAD_TS), thread);
});

test("repair setters are a no op for an unknown thread", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, dir);
  store.replaceOpenReply(CHANNEL, THREAD_TS, null, "ts-1");
  store.addRequest(CHANNEL, THREAD_TS, "ts-1");
  store.setStatusPending(CHANNEL, THREAD_TS, "x");
  assert.equal(store.thread(CHANNEL, THREAD_TS), null);
});

test("repairs pending lists only threads with something open", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.openThread(CHANNEL, OTHER_THREAD_TS);
  store.replaceOpenReply(CHANNEL, OTHER_THREAD_TS, null, "ts-1");
  assert.deepEqual(store.repairsPending(), [
    [CHANNEL, OTHER_THREAD_TS, mustThread(store, CHANNEL, OTHER_THREAD_TS)],
  ]);
});

test("clear repair fields clears all three and is a no op after", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.replaceOpenReply(CHANNEL, THREAD_TS, null, "ts-1");
  store.addRequest(CHANNEL, THREAD_TS, "ts-2");
  store.setStatusPending(CHANNEL, THREAD_TS, "x");
  store.clearRepair(CHANNEL, THREAD_TS);
  assert.deepEqual(store.thread(CHANNEL, THREAD_TS), threadState(join(dir, "project")));
  assert.deepEqual(
    new StateStore(path).thread(CHANNEL, THREAD_TS),
    threadState(join(dir, "project")),
  );
  const before = statSync(path, { bigint: true });
  store.clearRepair(CHANNEL, THREAD_TS); // already clear: a no-op, no extra write
  store.clearRepair("unknown-channel", THREAD_TS); // unknown thread: a no-op too
  assert.equal(statSync(path, { bigint: true }).ino, before.ino);
});

test("clear repair keeps what an unlanded answer still needs", () => {
  // An answer that never reached Slack leaves its open stream and the root's status for the
  // next start's repair; the requests, which a close deletes itself, are cleared.
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.replaceOpenReply(CHANNEL, THREAD_TS, null, "ts-1");
  store.addRequest(CHANNEL, THREAD_TS, "ts-2");
  store.setStatusPending(CHANNEL, THREAD_TS, "x");
  store.clearRepair(CHANNEL, THREAD_TS, { keepOpen: true });
  const kept = new StateStore(path).thread(CHANNEL, THREAD_TS);
  assert.deepEqual(kept, threadState(join(dir, "project"), { openReplies: ["ts-1"], status: "x" }));
});

test("prune keeps every thread of a folder it cannot decide", () => {
  // `alive` returns null when it cannot tell which sessions a folder holds: pruning errs on
  // keeping, so none of that folder's threads is removed.
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind("C000CHAN", join(dir, "undecided"));
  store.openThread("C000CHAN", "1700000000.000100", "kept-1");
  store.bind("C000CHAN", join(dir, "decided"));
  store.openThread("C000CHAN", "1700000000.000200", "gone-1");

  const alive = (directory: string): Set<string> | null =>
    directory.endsWith("undecided") ? null : new Set();

  assert.equal(store.prune(alive, 1700000000.0), 1);
  assert.notEqual(store.thread("C000CHAN", "1700000000.000100"), null);
  assert.equal(store.thread("C000CHAN", "1700000000.000200"), null);
});

test("an explicit off is kept apart from never chosen", () => {
  // `bypass` keeps its old meaning (`true` on, `false` not on); an explicit off (`!bypass off`
  // or an unticked Start) adds `bypass_off: true`, which must survive a restart. The two keys are
  // never both set, and clearing to unset removes `bypass_off`.
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  assert.equal(mustThread(store).bypass, null);
  assert.ok(written(path).bypass === false && !("bypass_off" in written(path)));
  store.setBypass(CHANNEL, THREAD_TS, false);
  assert.equal(mustThread(new StateStore(path)).bypass, false);
  assert.ok(written(path).bypass === false && written(path).bypass_off === true);
  store.setBypass(CHANNEL, THREAD_TS, true);
  assert.equal(mustThread(new StateStore(path)).bypass, true);
  assert.ok(written(path).bypass === true && !("bypass_off" in written(path)));
  store.setBypass(CHANNEL, THREAD_TS, null);
  assert.equal(mustThread(new StateStore(path)).bypass, null);
  assert.ok(written(path).bypass === false && !("bypass_off" in written(path)));
  assert.equal(read(path).version, 2);
});

for (const [keys, expected] of [
  [{ bypass: true }, true],
  [{ bypass: false }, null], // an old file: a thread that never chose
  [{}, null],
  [{ bypass: false, bypass_off: true }, false],
  [{ bypass_off: true }, false],
] as const) {
  test(`bypass is read from the two keys [${JSON.stringify(keys)}]`, () => {
    const dir = scratch();
    const path = join(dir, "state.json");
    writeThreadFile(path, dir, { directory: dir, session_id: null, effort: null, ...keys });
    assert.equal(mustThread(new StateStore(path)).bypass, expected);
  });
}

test("a written off reads as not on through bypass alone", () => {
  // The old code looked at `bypass is True` and ignored every other key.
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.setBypass(CHANNEL, THREAD_TS, false);
  assert.notEqual(written(path).bypass, true);
});

test("the ended reaction round trips and a pending one replaces it", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);

  store.setStatusPending(CHANNEL, THREAD_TS, null, "white_check_mark");
  assert.equal(written(path).status, null);
  assert.equal(written(path).ended, "white_check_mark");
  assert.equal(mustThread(new StateStore(path)).ended, "white_check_mark");

  store.setStatusPending(CHANNEL, THREAD_TS, "hourglass_flowing_sand");
  assert.equal(written(path).status, "hourglass_flowing_sand");
  assert.equal(written(path).ended, null);
});

test("a cross shown over a kept status keeps both", () => {
  // An answer that never reached Slack: the root shows ❌ and crash repair still owes it.
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.setStatusPending(CHANNEL, THREAD_TS, "hourglass_flowing_sand", "x");
  assert.deepEqual([written(path).status, written(path).ended], ["hourglass_flowing_sand", "x"]);
  assert.deepEqual(
    store.repairsPending().map(([, ts]) => ts),
    [THREAD_TS],
  );
});

test("an observer that raises does not break the write", () => {
  const dir = scratch();
  const warnings: string[] = [];
  const store = new StateStore(join(dir, "state.json"), { warn: (m) => warnings.push(m) });
  store.bind(CHANNEL, join(dir, "project"));

  store.onSessionsChange = () => {
    throw new TypeError("no running event loop");
  };
  store.openThread(CHANNEL, THREAD_TS);
  assert.notEqual(store.thread(CHANNEL, THREAD_TS), null);
  assert.ok(warnings.some((message) => message.includes("TypeError")));
  assert.ok(warnings.every((message) => !message.includes("no running event loop")));
});

test("an ended reaction is nothing for crash repair to find", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.setStatusPending(CHANNEL, THREAD_TS, null, "x");
  assert.deepEqual(store.repairsPending(), []);
});

test("clear repair keeps the ended reaction", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS);
  store.setStatusPending(CHANNEL, THREAD_TS, null, "x");
  store.addRequest(CHANNEL, THREAD_TS, "1790549807.000001");
  store.clearRepair(CHANNEL, THREAD_TS);
  assert.equal(mustThread(store).ended, "x");
});

test("a file without the ended key loads with none", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  writeThreadFile(path, dir, { directory: dir, session_id: SESSION, status: null });
  assert.equal(mustThread(new StateStore(path)).ended, null);
});

test("threads lists every thread of every channel", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.bind(OTHER_CHANNEL, join(dir, "other"));
  const first = store.openThread(CHANNEL, THREAD_TS);
  const second = store.openThread(OTHER_CHANNEL, OTHER_THREAD_TS);
  assert.deepEqual(store.threads(), [
    [CHANNEL, THREAD_TS, first],
    [OTHER_CHANNEL, OTHER_THREAD_TS, second],
  ]);
});

test("the observer hears what the session index shows and nothing else", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  const heard: (readonly ThreadKey[])[] = [];
  store.onSessionsChange = (changed) => heard.push(changed);

  store.bind(CHANNEL, join(dir, "project")); // a new channel is a new group on the page
  assert.deepEqual(store.channels(), [CHANNEL]);
  assert.deepEqual(heard, [[]]); // no thread of its own yet
  store.openThread(CHANNEL, THREAD_TS);
  store.openThread(CHANNEL, OTHER_THREAD_TS);
  store.setSession(CHANNEL, THREAD_TS, SESSION);
  store.setStatusPending(CHANNEL, THREAD_TS, "hourglass_flowing_sand");
  store.setStatusPending(CHANNEL, THREAD_TS, null, "white_check_mark");
  assert.equal(heard.length, 6);
  // Each time, the thread the write touched and no other.
  assert.deepEqual(heard[1], [[CHANNEL, THREAD_TS]]);
  assert.deepEqual(heard[2], [[CHANNEL, OTHER_THREAD_TS]]);
  for (const changed of heard.slice(3)) assert.deepEqual(changed, [[CHANNEL, THREAD_TS]]);

  // What no row of the index shows: a reply's bookkeeping, a request, bypass, effort, a rebind.
  store.replaceOpenReply(CHANNEL, THREAD_TS, null, "1790549807.000001");
  store.addRequest(CHANNEL, THREAD_TS, "1790549807.000002");
  store.setBypass(CHANNEL, THREAD_TS, true);
  store.setEffort(CHANNEL, THREAD_TS, "low");
  store.bind(CHANNEL, join(dir, "elsewhere"));
  assert.equal(heard.length, 6);

  store.removeThread(CHANNEL, THREAD_TS);
  assert.deepEqual(heard.slice(6), [[[CHANNEL, THREAD_TS]]]);
});

test("the observer hears a prune that removed something", () => {
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS, SESSION);
  const heard: (readonly ThreadKey[])[] = [];
  store.onSessionsChange = (changed) => heard.push(changed);

  assert.equal(
    store.prune(() => new Set([SESSION]), Number(THREAD_TS)),
    0,
  );
  assert.deepEqual(heard, []);
  assert.equal(
    store.prune(() => new Set(), Number(THREAD_TS)),
    1,
  );
  assert.deepEqual(heard, [[[CHANNEL, THREAD_TS]]]);
});

test("remove channel forgets the channel and its threads and says which", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const store = new StateStore(path);
  store.bind(CHANNEL, join(dir, "project"));
  store.bind(OTHER_CHANNEL, join(dir, "other"));
  store.openThread(CHANNEL, THREAD_TS, SESSION);
  store.openThread(OTHER_CHANNEL, OTHER_THREAD_TS);
  const heard: (readonly ThreadKey[])[] = [];
  store.onSessionsChange = (changed) => heard.push(changed);

  store.removeChannel(CHANNEL);
  assert.deepEqual(store.channels(), [OTHER_CHANNEL]);
  assert.equal(store.thread(CHANNEL, THREAD_TS), null);
  assert.deepEqual(Object.keys(read(path).channels), [OTHER_CHANNEL]);
  assert.deepEqual(heard, [[[CHANNEL, THREAD_TS]]]);
  assert.deepEqual(new StateStore(path).channels(), [OTHER_CHANNEL]); // it stays forgotten after a restart

  store.removeChannel(CHANNEL); // not there any more: nothing written, nothing announced
  assert.deepEqual(heard, [[[CHANNEL, THREAD_TS]]]);
});

test("prune leaves alone the threads it is told to keep", () => {
  // A thread with a live session in the daemon: its session id may not be on disk yet, and
  // its setup may have been open for more than a day.
  const dir = scratch();
  const store = new StateStore(join(dir, "state.json"));
  store.bind(CHANNEL, join(dir, "project"));
  store.openThread(CHANNEL, THREAD_TS, SESSION);
  store.openThread(CHANNEL, OTHER_THREAD_TS);
  const later = Number(OTHER_THREAD_TS) + 2 * 24 * 60 * 60;
  const keep: ThreadKey[] = [
    [CHANNEL, THREAD_TS],
    [CHANNEL, OTHER_THREAD_TS],
  ];
  assert.equal(
    store.prune(() => new Set(), later, keep),
    0,
  );
  assert.equal(
    store.prune(() => new Set(), later, [[CHANNEL, THREAD_TS]]),
    1,
  );
  assert.notEqual(store.thread(CHANNEL, THREAD_TS), null);
  assert.equal(store.thread(CHANNEL, OTHER_THREAD_TS), null);
});

// The cases below have no Python test: they pin the bytes and the readers the port must keep.

// What the Python store wrote for the same calls (`json.dump(data, f, indent=2)`, captured from
// `awaydesk.state` on 2026-10-10): two-space indent, key order of the code, every non-ASCII
// character and DEL escaped, empty containers as `[]` and `{}`, and no trailing newline.
const PYTHON_BYTES = String.raw`{
  "version": 2,
  "channels": {
    "C000CHAN": {
      "directory": "/work/caf\u00e9 \ud83d\ude80 \"q\" \\ \t\u007f",
      "notice_pending": false,
      "threads": {
        "1790549806.565369": {
          "directory": "/work/caf\u00e9 \ud83d\ude80 \"q\" \\ \t\u007f",
          "session_id": "s-1",
          "bypass": true,
          "effort": "high",
          "open_replies": [
            "1790549807.000001",
            "1790549807.000009"
          ],
          "requests": [
            "1790549807.000002"
          ],
          "status": "hourglass_flowing_sand",
          "ended": null
        },
        "1790549900.100000": {
          "directory": "/work/caf\u00e9 \ud83d\ude80 \"q\" \\ \t\u007f",
          "session_id": "s-\u00e9\u2028",
          "bypass": false,
          "bypass_off": true,
          "effort": null,
          "open_replies": [],
          "requests": [],
          "status": null,
          "ended": null
        }
      }
    },
    "C000EMPT": {
      "directory": "/work/empty",
      "notice_pending": false,
      "threads": {}
    },
    "C000OTHR": {
      "directory": "/work/other",
      "notice_pending": false,
      "threads": {
        "1790550000.000001": {
          "directory": "/work/other",
          "session_id": null,
          "bypass": false,
          "effort": null,
          "open_replies": [],
          "requests": [],
          "status": null,
          "ended": "white_check_mark"
        }
      }
    }
  }
}`;

test("the file is written byte for byte as the Python store wrote it", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const [c, e, o] = ["C000CHAN", "C000EMPT", "C000OTHR"];
  const [t1, t2, t3] = [THREAD_TS, OTHER_THREAD_TS, "1790550000.000001"];
  const store = new StateStore(path);
  store.bind(c, '/work/café \u{1F680} "q" \\ \t\x7f');
  store.bind(e, "/work/empty");
  store.bind(o, "/work/other");
  store.openThread(c, t1, "s-1");
  store.openThread(c, t2);
  store.openThread(o, t3);
  store.setBypass(c, t1, true);
  store.setBypass(c, t2, false);
  store.setEffort(c, t1, "high");
  store.replaceOpenReply(c, t1, null, "1790549807.000001");
  store.replaceOpenReply(c, t1, null, "1790549807.000009");
  store.addRequest(c, t1, "1790549807.000002");
  store.setStatusPending(c, t1, "hourglass_flowing_sand");
  store.setStatusPending(o, t3, null, "white_check_mark");
  store.setSession(c, t2, "s-é ");
  assert.equal(readFileSync(path, "utf8"), PYTHON_BYTES);
  // And the Python bytes read back into the same state.
  assert.deepEqual(new StateStore(path).threads(), store.threads());
});

test("a file with an unreadable shape is refused with the file named", () => {
  const dir = scratch();
  const path = join(dir, "state.json");
  const shapes: unknown[] = [
    [],
    null,
    { channels: {} },
    { version: 2 },
    { version: 2, channels: [] },
    { version: 2, channels: { [CHANNEL]: null } },
    { version: 2, channels: { [CHANNEL]: { notice_pending: false } } },
    { version: 2, channels: { [CHANNEL]: { directory: 7 } } },
    { version: 2, channels: { [CHANNEL]: { directory: dir, threads: null } } },
    { version: 2, channels: { [CHANNEL]: { directory: dir, threads: { [THREAD_TS]: {} } } } },
    { version: 1, channels: { [CHANNEL]: {} } },
  ];
  for (const shape of shapes) {
    writeFileSync(path, JSON.stringify(shape));
    assert.throws(
      () => new StateStore(path),
      (error: unknown) =>
        error instanceof StateError && error.message.startsWith(`${path} cannot be read (`),
      JSON.stringify(shape),
    );
  }
});

test("a file that is not UTF-8 is refused, not repaired", () => {
  const path = join(scratch(), "state.json");
  writeFileSync(path, Buffer.from([0x7b, 0xff, 0x7d]));
  assert.throws(() => new StateStore(path), StateError);
});
