/**
 * `!open`, through Bolt's own entry point. Port of `tests/test_slack_app.py` from its `!open`
 * section to the log lines of issue #142 (which are `slack-app-clip.test.ts`): the picker's button
 * and modal, a name typed in the thread, and what nobody but the owner may do with either.
 *
 * Where Python replaced a function of the daemon (`trusted_repository`, `Listings.of`,
 * `walk_files`, `_locate`, `OPEN_WAIT`) these tests stand at the same place: the lookup the world's
 * back end answers with (`world.backend.repository`), a `Listings` of their own over it, and the
 * handlers' clock (`world.appClock`), whose `OPEN_WAIT` is crossed by advancing it. A wait for
 * the daemon to come to rest is `world.idle()` or `world.settle()`; nothing waits on the wall
 * clock.
 */
import assert from "node:assert/strict";
import {
  mkdirSync,
  realpathSync,
  symlinkSync,
  unlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { type TestContext, test } from "node:test";
import type { Repository } from "../../../../src/agent/seam.ts";
import { actionKey } from "../../../../src/chat/slack/app/app.ts";
import {
  type Listing,
  Listings,
  type RepositoryLookup,
} from "../../../../src/chat/slack/openfile/listing.ts";
import {
  modalView,
  OPEN_FORM,
  OPEN_WAIT,
  QUERY_ACTION,
  QUERY_BLOCK,
  Target,
} from "../../../../src/chat/slack/openfile/modal.ts";
import { regularFiles } from "../../../../src/chat/slack/openfile/read.ts";
import * as texts from "../../../../src/core/texts.ts";
import { fill } from "../../../../src/core/texts.ts";
import {
  type Args,
  AsyncEvent,
  BOT,
  CHANNEL,
  FakeClock,
  OTHER_TEAM,
  OTHER_THREAD,
  OWNER,
  STRANGER,
  TEAM,
  THREAD,
} from "../../../support/fake-slack.ts";
import { commitAt, committed, git, gitInit } from "../../../support/git-layouts.ts";
import { GIT_LAYOUT } from "../../../support/platform.ts";
import {
  askedOpen,
  type Body,
  chooseClick,
  inAThread,
  ModalSlack,
  madeBeforeTheThread,
  message,
  OPEN_TARGET,
  opened,
  pickerPosts,
  projectOf,
  put,
  reply,
  said,
  shownChoiceId,
  submitted,
  typing,
  type World,
  type WorldOptions,
  worldFor,
} from "../../../support/slack-app.ts";

// A symbolic link is a POSIX matter; Windows is not a supported host.
const SYMLINKS = { skip: process.platform === "win32" ? "links are read by POSIX rules" : false };

// Each pair is a user who is not the owner, and the owner from another workspace.
const ANYONE_ELSE: ReadonlyArray<readonly [user: string, team: string]> = [
  [STRANGER, TEAM],
  [OWNER, OTHER_TEAM],
];

/**
 * A timer that keeps the event loop alive for the test's duration: on Node 22 a test that awaits
 * something which never comes, with only unref'd timers pending, lets the process finish and
 * cancels the rest of the file (the sessions harness does the same; the world does not).
 */
function keepAlive(t: TestContext): void {
  const guard = setInterval(() => {}, 1_000);
  t.after(() => clearInterval(guard));
}

function openWorld(t: TestContext, options: WorldOptions = {}): World {
  keepAlive(t);
  return worldFor(t)(options);
}

/**
 * A world whose `!open` listings are `listings`, which is made over the lookup the world's
 * sessions answer with: the world needs the listings to be made, and they need the world to look
 * a repository up.
 */
function worldWith<L extends Listings>(
  t: TestContext,
  make: (lookup: RepositoryLookup) => L,
): World {
  keepAlive(t);
  let world: World | undefined;
  const listings = make((directory, sessionFolder) => {
    if (world === undefined) throw new Error("the world is not made yet");
    return world.sessions.repository(directory, sessionFolder);
  });
  world = worldFor(t)({ listings });
  return world;
}

/** Python's `Listings.of` patched to note the folders it was asked for, and answer as before. */
class WatchedListings extends Listings {
  readonly listed: string[] = [];

  override of(folder: string, options: { again?: boolean; signal?: AbortSignal } = {}) {
    this.listed.push(folder);
    return super.of(folder, options);
  }
}

/** A walk of a plain folder that ran out of time with `found` in hand (Python's `cut_walk`). */
class CutListings extends Listings {
  readonly #found: readonly string[];
  #made = false;

  constructor(lookup: RepositoryLookup, found: readonly string[]) {
    super(lookup);
    this.#found = found;
  }

  override async of(
    _folder: string,
    options: { again?: boolean; signal?: AbortSignal } = {},
  ): Promise<Listing> {
    // The first listing is made for the request that gets it; the next one is the one kept.
    const fresh = !this.#made || options.again === true;
    this.#made = true;
    return { files: this.#found, complete: false, fresh };
  }
}

/** The lookup of the world's sessions, counting the folders it was asked about. */
function counted(world: World): string[] {
  const asked: string[] = [];
  const real = world.backend.repository;
  world.backend.repository = (directory, sessionFolder) => {
    asked.push(directory);
    return real(directory, sessionFolder);
  };
  return asked;
}

/** The lookup of the world's sessions, held until `release` is set (a repository that is slow). */
function slowLookup(world: World, release: AsyncEvent): void {
  const real = world.backend.repository;
  world.backend.repository = async (directory, sessionFolder): Promise<Repository | null> => {
    await release.wait();
    return real(directory, sessionFolder);
  };
}

function only<T>(items: readonly T[]): T {
  assert.equal(items.length, 1);
  return items[0] as T;
}

function calls(world: World, method: string): Body[] {
  return world.slack.callsTo(method) as Body[];
}

function threeFiles(project: string): void {
  for (const name of ["one.py", "onetwo.py", "onetwothree.py"]) put(project, name);
}

test(
  "two replies sent together reach the queue in the order they were sent",
  GIT_LAYOUT,
  async (t) => {
    // Nothing of `!open` runs when a prompt arrives: a slow repository lookup (a cold cache, a
    // slow git) must not hold a prompt, or let a later one overtake it. With a lookup awaited
    // before the arrival lock, the first reply here is overtaken by the second.
    const world = openWorld(t);
    projectOf(world);
    await inAThread(world);
    const session = world.sessions.get(CHANNEL, THREAD);
    assert.ok(session !== null);
    const queued: string[] = [];
    const submit = session.submit.bind(session);
    session.submit = async (prompt) => {
      queued.push(String(prompt));
      return submit(prompt);
    };
    const asked = counted(world);
    const real = world.backend.repository;
    const slow = new AsyncEvent();
    let first = true;
    world.backend.repository = async (directory, sessionFolder) => {
      if (first) {
        first = false;
        await slow.wait();
      }
      return real(directory, sessionFolder);
    };
    await world.dispatch(reply("first", THREAD));
    await world.dispatch(reply("second", THREAD));
    slow.set();
    await world.settle();
    assert.deepEqual(queued, ["first", "second"]);
    const lookedUp = asked.length; // the replies' own footers look a repository up when they end
    await askedOpen(world, "!open zzz");
    assert.ok(asked.length > lookedUp); // the control: the lookup is counted, and a name makes one
  },
);

test("open alone posts a button and reads nothing of the folder", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  projectOf(world);
  await inAThread(world);
  const asked = counted(world);
  await askedOpen(world, "!open");
  const post = only(pickerPosts(world));
  assert.ok(post.thread_ts === THREAD && post.channel === CHANNEL);
  const [section, actions, context] = post.blocks as [Body, Body, Body];
  assert.equal(section.text.text, "*Open a file*");
  const button = only(actions.elements as Body[]);
  assert.ok(button.type === "button" && button.text.text === "Choose a file");
  assert.ok(!("value" in button));
  assert.equal(context.elements[0].text, "Or type `!open setup` to open a file by name.");
  // The rows are read when the button is clicked, not when the message is posted.
  assert.ok(asked.length === 0 && calls(world, "views.open").length === 0);
});

test("the button opens the modal with the files changed in the session", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  const committedSince = put(project, "docs/guide.md");
  git(project, "add", "-A");
  git(project, "commit", "-q", "-m", "docs");
  const older = put(project, "notes.txt");
  utimesSync(committedSince, 1_790_000_100, 1_790_000_100);
  utimesSync(older, 1_790_000_000, 1_790_000_000);
  const body = chooseClick();
  await world.dispatch(body);
  await world.settle();
  const asked = only(calls(world, "views.open"));
  assert.equal(asked.trigger_id, body.trigger_id);
  assert.deepEqual(modal.rows(), ["docs/guide.md", "notes.txt"]);
  assert.deepEqual(modal.labels(), ["Changed in this session (2), newest first"]);
  assert.equal(modal.view.callback_id, OPEN_FORM);
  assert.deepEqual(Target.load(modal.view.private_metadata), OPEN_TARGET);
  assert.equal(modal.view.blocks[0].element.focus_on_load, true);
  assert.ok(!("initial_value" in modal.view.blocks[0].element));
  assert.equal(modal.calls.length, 0); // the rows were ready: no update after it
  assert.deepEqual(world.ephemerals(), []);
});

test("a session that changed nothing gets the line that says to type", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  await world.dispatch(chooseClick());
  await world.settle();
  assert.deepEqual(modal.rows(), []);
  assert.deepEqual(modal.labels(), [texts.OPEN_TYPE_A_NAME]);
});

test(
  "the changes are counted from where head was when the thread started",
  GIT_LAYOUT,
  async (t) => {
    // Nothing is remembered by the daemon: HEAD's log says where the repository stood at THREAD's
    // time (an epoch second), so a daemon restarted since gives the same answer.
    const world = openWorld(t);
    const modal = new ModalSlack(world.slack);
    const folder = join(world.root, "app");
    const start = Number(THREAD.split(".")[0]);
    const repo = realpathSync(gitInit(join(folder, "workspace")));
    commitAt(repo, "base.py", start - 1000);
    commitAt(repo, "before.py", start - 500); // moved HEAD before the thread began
    commitAt(repo, "made_by_claude.py", start + 500);
    put(repo, "uncommitted.py");
    await inAThread(world);
    await world.dispatch(chooseClick());
    await world.settle();
    assert.deepEqual(modal.rows().toSorted(), [
      "workspace/made_by_claude.py",
      "workspace/uncommitted.py",
    ]);
  },
);

test(
  "a repository made after the thread began lists every file as changed",
  GIT_LAYOUT,
  async (t) => {
    const world = openWorld(t);
    const modal = new ModalSlack(world.slack);
    await inAThread(world);
    const made = committed(join(world.root, "app")); // THREAD is long past: the first commit is the session's
    put(made, "new.py");
    await world.dispatch(chooseClick());
    await world.settle();
    assert.deepEqual(modal.rows().toSorted(), ["README", "new.py"]);
  },
);

test("a folder that is not a repository has no changed files", async (t) => {
  const world = openWorld(t);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  put(join(world.root, "app"), "plain.txt");
  await world.dispatch(chooseClick());
  await world.settle();
  assert.ok(modal.rows().length === 0);
  assert.deepEqual(modal.labels(), [texts.OPEN_TYPE_A_NAME]);
});

test("the repositories of a folder are looked up once for a short while", GIT_LAYOUT, async (t) => {
  // The listings keep them for REPOSITORIES_TTL seconds: of a clock that does not move, so
  // that a slow machine cannot make the second click come too late.
  const world = worldWith(t, (lookup) => new Listings(lookup, { clock: new FakeClock() }));
  new ModalSlack(world.slack);
  const folder = join(world.root, "app");
  madeBeforeTheThread(join(folder, "workspace"));
  await inAThread(world);
  const asked = counted(world);
  await world.dispatch(chooseClick());
  await world.settle();
  const first = asked.length;
  assert.ok(first > 0);
  await world.dispatch(chooseClick());
  await world.settle();
  assert.equal(asked.length, first); // the second click found them kept
});

test("the changes of a repository inside the folder are listed", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  // The channel's folder is a plain folder that holds a repository one level down.
  const folder = join(dirname(project), "plain");
  mkdirSync(folder);
  world.state.bind(CHANNEL, folder);
  const nested = madeBeforeTheThread(join(folder, "workspace"));
  await inAThread(world);
  put(nested, "docs/new.md");
  put(folder, "notes.txt"); // outside any repository: not a change
  await world.dispatch(chooseClick());
  await world.settle();
  assert.deepEqual(modal.rows(), ["workspace/docs/new.md"]);
  assert.deepEqual(modal.labels(), ["Changed in this session (1), newest first"]);
});

test("a name that matches several files posts the count and the button", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  await inAThread(world);
  for (const name of ["docs/setup.md", "src/setup.py", "setup/readme.md"]) put(project, name);
  await askedOpen(world, "!open setup");
  assert.deepEqual(opened(world), []);
  const post = only(pickerPosts(world));
  assert.equal(post.thread_ts, THREAD);
  const [section, actions] = post.blocks as [Body, Body];
  assert.equal(section.text.text, "*3 files match* `setup`");
  const button = only(actions.elements as Body[]);
  assert.ok(button.text.text === "Choose a file" && button.value === "setup");
  assert.deepEqual(calls(world, "views.open"), []); // the rows come when it is clicked
});

test("the button of several matches opens the modal on them", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  for (const name of ["docs/setup.md", "src/setup.py", "setup/readme.md"]) put(project, name);
  await world.dispatch(chooseClick("setup"));
  await world.settle();
  const field = modal.view.blocks[0].element;
  assert.equal(field.initial_value, "setup");
  // The file names first, then the folder's file.
  assert.deepEqual(modal.rows(), ["src/setup.py", "docs/setup.md", "setup/readme.md"]);
  assert.deepEqual(modal.labels(), ["3 files match"]);
});

test("the modal of a plain folder lists what its disk holds", async (t) => {
  const world = openWorld(t);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  for (const name of ["a/setup.md", "b/setup.py"]) put(join(world.root, "app"), name);
  await askedOpen(world, "!open setup");
  const post = only(pickerPosts(world));
  await world.dispatch(chooseClick(post.blocks[1].elements[0].value));
  await world.settle();
  assert.deepEqual(modal.rows().toSorted(), ["a/setup.md", "b/setup.py"]);
});

test("more than ten matches list ten and say so", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  for (let i = 0; i < 130; i += 1) put(project, `data/part${String(i).padStart(3, "0")}.csv`);
  await world.dispatch(chooseClick("part"));
  await world.settle();
  assert.equal(modal.rows().length, 10);
  assert.deepEqual(modal.labels(), [
    "130 files match",
    fill(texts.OPEN_MATCHES_CAPPED, { shown: 10, count: 130 }),
  ]);
});

test("a listing that comes late fills the modal after it opens", GIT_LAYOUT, async (t) => {
  // The click's trigger_id lives 3 seconds (views.open reference): the modal opens without
  // the rows when they are not ready, and an update fills it.
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  put(project, "src/parser.py");
  const release = new AsyncEvent();
  slowLookup(world, release);
  await world.dispatch(chooseClick("parser"));
  await world.appClock.advance(OPEN_WAIT);
  await world.idle();
  assert.deepEqual(modal.labels(), [texts.OPEN_LOADING]);
  assert.equal(modal.written.length, 0);
  assert.equal(modal.view.blocks[0].element.initial_value, "parser");
  release.set();
  await world.settle();
  assert.deepEqual(modal.rows(), ["src/parser.py"]);
  assert.equal(modal.written.length, 1);
  assert.equal(modal.calls[0]?.[0], null); // no hash: the daemon is the view's only writer
  assert.ok(!("initial_value" in modal.view.blocks[0].element)); // what was typed stays
});

test(
  "a keystroke typed while the modal is still filling is not overwritten",
  GIT_LAYOUT,
  async (t) => {
    const world = openWorld(t);
    const project = projectOf(world);
    const modal = new ModalSlack(world.slack);
    await inAThread(world);
    put(project, "src/parser.py");
    put(project, "src/lexer.py");
    const release = new AsyncEvent();
    slowLookup(world, release);
    await world.dispatch(chooseClick("src"));
    await world.appClock.advance(OPEN_WAIT);
    await world.idle();
    await world.dispatch(typing("lex", 1790000100.0, { viewHash: modal.hash }));
    release.set();
    await world.settle();
    assert.deepEqual(modal.rows(), ["src/lexer.py"]);
    assert.deepEqual(
      modal.calls.map(([, view]) => modal.rows(view)),
      [["src/lexer.py"]],
    );
  },
);

test("the files are listed while the channel is checked", GIT_LAYOUT, async (t) => {
  // The checks before the modal make two calls to Slack; the listing does not wait for them.
  const world = openWorld(t);
  projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  const listingStarted = new AsyncEvent();
  const real = world.backend.repository;
  world.backend.repository = (directory, sessionFolder) => {
    listingStarted.set();
    return real(directory, sessionFolder);
  };
  const gate = new AsyncEvent();
  world.slack.gate = gate;
  world.slack.gateMethod = "conversations.info";
  await world.dispatch(chooseClick());
  assert.ok(world.slack.gated.isSet());
  assert.ok(listingStarted.isSet()); // while the channel check is still waiting
  assert.deepEqual(calls(world, "views.open"), []);
  gate.set();
  await world.settle();
  assert.deepEqual(modal.rows(), []); // nothing changed in this session
  const asked = only(calls(world, "views.open"));
  assert.deepEqual(modal.labels(), [texts.OPEN_TYPE_A_NAME]);
  assert.ok(asked.trigger_id);
});

test(
  "the wait for the rows counts from the click and not from the channel check",
  GIT_LAYOUT,
  async (t) => {
    // The click's trigger_id lives 3 seconds: the check, the wait and views.open share them.
    const world = openWorld(t);
    const project = projectOf(world);
    const modal = new ModalSlack(world.slack);
    await inAThread(world);
    put(project, "src/parser.py");
    const release = new AsyncEvent();
    slowLookup(world, release);
    const openedAt: number[] = [];
    const opening = world.slack.responses["views.open"];
    assert.equal(typeof opening, "function");
    world.slack.responses["views.open"] = (args: Args) => {
      openedAt.push(world.appClock.time());
      return (opening as (args: Args) => Args)(args);
    };
    const gate = new AsyncEvent();
    world.slack.gate = gate;
    world.slack.gateMethod = "conversations.info";
    await world.dispatch(chooseClick("parser"));
    await world.appClock.advance(0.25); // the channel check takes this long
    gate.set();
    await world.idle();
    // The wait is what is left of OPEN_WAIT, not a whole one from the check.
    await world.appClock.advance(0.5);
    assert.deepEqual(openedAt, []);
    await world.appClock.advance(0.25);
    await world.idle();
    assert.deepEqual(openedAt, [OPEN_WAIT]);
    release.set();
    await world.settle();
    assert.deepEqual(modal.labels(), ["1 file matches"]);
    assert.deepEqual(modal.rows(), ["src/parser.py"]);
  },
);

test("a form that cannot open tells the owner of the picker", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  projectOf(world);
  await inAThread(world);
  world.slack.responses["views.open"] = { ok: false, error: "expired_trigger_id" };
  await world.dispatch(chooseClick());
  await world.settle();
  assert.deepEqual(world.ephemerals(), [
    fill(texts.OPEN_FORM_NOT_OPENED, { error: "expired_trigger_id" }),
  ]);
});

for (const [user, team] of ANYONE_ELSE) {
  test(
    `the button clicked by anyone else opens nothing [${user}-${team}]`,
    GIT_LAYOUT,
    async (t) => {
      const world = openWorld(t);
      projectOf(world);
      new ModalSlack(world.slack);
      await inAThread(world);
      const asked = counted(world);
      const body = chooseClick("", { id: user });
      body.team.id = team;
      await world.dispatch(body);
      await world.settle();
      assert.deepEqual(calls(world, "views.open"), []);
      assert.deepEqual(world.ephemerals(), []);
      assert.deepEqual(asked, []); // the listing that starts early is the owner's alone
    },
  );
}

test("the button in a thread that holds no session opens nothing", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  projectOf(world);
  new ModalSlack(world.slack);
  await world.dispatch(chooseClick());
  assert.deepEqual(calls(world, "views.open"), []);
  assert.deepEqual(world.ephemerals(), [texts.NOT_A_SESSION]);
});

test("the button in a refused channel opens nothing", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  projectOf(world);
  new ModalSlack(world.slack);
  await inAThread(world);
  world.slack.responses["conversations.members"] = { ok: true, members: [OWNER, BOT, STRANGER] };
  await world.dispatch(chooseClick());
  assert.deepEqual(calls(world, "views.open"), []);
  assert.deepEqual(world.ephemerals(), [
    fill(texts.CHANNEL_REFUSED, { reason: texts.REASON_MEMBERS }),
  ]);
});

test("typing puts the files that match into the rows", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  put(project, "src/parser.py");
  put(project, "src/lexer.py");
  put(project, ".gitignore", "*.log\n");
  put(project, "trace.log");
  const shown = modal.hash;
  await world.dispatch(typing("PARS", 1790000100.0, { viewHash: shown }));
  await world.settle();
  assert.deepEqual(modal.rows(), ["src/parser.py"]);
  assert.deepEqual(modal.labels(), ["1 file matches"]);
  assert.equal(modal.calls[0]?.[0], null); // the hash is optional, and the daemon's ordering decides
  // What the update sends keeps the field's ids and does not restate its text.
  const field = modal.view.blocks[0];
  assert.deepEqual([field.block_id, field.element.action_id], [QUERY_BLOCK, QUERY_ACTION]);
  assert.ok(!("initial_value" in field.element));
  await world.dispatch(typing("trace", 1790000101.0, { viewHash: modal.hash })); // there, and ignored
  await world.settle();
  assert.deepEqual(modal.rows(), []);
  assert.deepEqual(modal.labels(), ["0 files match"]);
});

test("the typed text is read from the state when the action has none", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  put(project, "src/parser.py");
  const body = typing("pars", 1790000100.0, { viewHash: modal.hash });
  delete body.actions[0].value;
  await world.dispatch(body);
  await world.settle();
  assert.deepEqual(modal.rows(), ["src/parser.py"]);
});

test("an emptied field lists the changed files again", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  put(project, "new.py");
  await world.dispatch(typing("", 1790000100.0, { viewHash: modal.hash }));
  await world.settle();
  assert.deepEqual(modal.rows(), ["new.py"]);
  assert.deepEqual(modal.labels(), ["Changed in this session (1), newest first"]);
});

test("typing lists at most ten files", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  for (let i = 0; i < 130; i += 1) put(project, `data/part${String(i).padStart(3, "0")}.csv`);
  await world.dispatch(typing("part", 1790000100.0, { viewHash: modal.hash }));
  await world.settle();
  assert.equal(modal.rows().length, 10);
  assert.equal(modal.labels()[0], "130 files match");
});

test("typing checks the disk only until the rows are full", GIT_LAYOUT, async (t) => {
  // The listings are asked to check ranked paths on disk through an iterable that notes each path
  // it hands out: Python counted the calls of `_locate`, one for each path checked.
  const checked: string[] = [];
  function* counting(relatives: Iterable<string>): Generator<string> {
    for (const relative of relatives) {
      checked.push(relative);
      yield relative;
    }
  }
  const world = worldWith(
    t,
    (lookup) =>
      new Listings(lookup, {
        regular: (folder, relatives, limit) => regularFiles(folder, counting(relatives), limit),
      }),
  );
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  for (let i = 0; i < 300; i += 1) put(project, `data/part${String(i).padStart(3, "0")}.csv`);
  await world.dispatch(typing("part", 1790000100.0));
  await world.settle();
  assert.equal(modal.rows().length, 10);
  assert.equal(checked.length, 10); // not one for each of the 300 matches
  // The count is the matches by name once ten files are there to show.
  assert.deepEqual(modal.labels(), [
    "300 files match",
    fill(texts.OPEN_MATCHES_CAPPED, { shown: 10, count: 300 }),
  ]);
});

test("a count below the rows is exact even when a match is gone", GIT_LAYOUT, async (t) => {
  // `git ls-files` still names a tracked file deleted from the work tree.
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  for (const name of ["keep-a.py", "keep-b.py", "gone.py"]) put(project, name);
  git(project, "add", "-A");
  git(project, "commit", "-q", "-m", "three");
  unlinkSync(join(project, "gone.py"));
  await world.dispatch(typing(".py", 1790000100.0));
  await world.settle();
  assert.deepEqual(modal.rows().toSorted(), ["keep-a.py", "keep-b.py"]);
  assert.deepEqual(modal.labels(), ["2 files match"]);
});

test("typing leaves out a file whose path is too long for a value", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  const longName = "z".repeat(140);
  put(project, `${longName}/${longName}.py`);
  put(project, "short_z.py");
  await world.dispatch(typing("z", 1790000100.0, { viewHash: modal.hash }));
  await world.settle();
  assert.deepEqual(modal.rows(), ["short_z.py"]);
});

test("typing in a folder with no repository reads the disk", async (t) => {
  const world = openWorld(t);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  const plain = join(world.root, "app");
  put(plain, "plain.txt");
  put(plain, "sub/deep/plain-too.txt");
  await world.dispatch(typing("plain", 1790000100.0, { viewHash: modal.hash }));
  await world.settle();
  assert.deepEqual(modal.rows().toSorted(), ["plain.txt", "sub/deep/plain-too.txt"]);
});

test(
  "typing in a repository inside the folder leaves out what git ignores",
  GIT_LAYOUT,
  async (t) => {
    const world = openWorld(t);
    const modal = new ModalSlack(world.slack);
    const folder = join(world.root, "app");
    const nested = realpathSync(committed(join(folder, "workspace")));
    put(nested, ".gitignore", ".venv/\n");
    put(nested, "docs/setup.md");
    put(nested, ".venv/lib/setup_tools.py");
    put(folder, "setup-notes.txt");
    await inAThread(world);
    await world.dispatch(typing("setup", 1790000100.0, { viewHash: modal.hash }));
    await world.settle();
    assert.deepEqual(modal.rows().toSorted(), ["setup-notes.txt", "workspace/docs/setup.md"]);
  },
);

test("typing makes no call to slack about the channel", GIT_LAYOUT, async (t) => {
  // Each character comes here: the checks are the owner and the workspace alone.
  const world = openWorld(t);
  projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  const before = world.slack.apiCalls.length;
  await world.dispatch(typing("read", 1790000100.0, { viewHash: modal.hash }));
  await world.settle();
  assert.deepEqual(modal.rows(), ["README"]);
  const methods = world.slack.apiCalls.slice(before).map((call) => call.method);
  assert.deepEqual(methods, ["views.update"]);
});

for (const [user, team] of ANYONE_ELSE) {
  test(`typing by anyone else updates nothing [${user}-${team}]`, GIT_LAYOUT, async (t) => {
    const world = openWorld(t);
    projectOf(world);
    const modal = new ModalSlack(world.slack);
    await inAThread(world);
    const before = world.slack.apiCalls.length;
    await world.dispatch(typing("READ", 1790000100.0, { user, team, viewHash: modal.hash }));
    await world.settle();
    assert.equal(modal.calls.length, 0);
    assert.equal(world.slack.apiCalls.length, before);
  });
}

for (const [name, metadata] of [
  ["empty", ""],
  ["not json", "not json"],
  ['{"c": 1}', '{"c": 1}'],
  ["a thread of another channel", new Target("C000ELSEWHERE", THREAD).dump()],
  ["a thread that is no session", new Target(CHANNEL, "1790000000.999999").dump()],
] as const) {
  test(
    `typing in a modal whose thread is not a session updates nothing [${name}]`,
    GIT_LAYOUT,
    async (t) => {
      const world = openWorld(t);
      projectOf(world);
      const modal = new ModalSlack(world.slack);
      await inAThread(world);
      await world.dispatch(typing("READ", 1790000100.0, { metadata, viewHash: modal.hash }));
      await world.settle();
      assert.equal(modal.calls.length, 0);
    },
  );
}

test("typing resolves in the folder of the thread s own session", GIT_LAYOUT, async (t) => {
  // D5: the channel was bound to another folder after the thread began.
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  const elsewhere = join(world.root, "elsewhere");
  put(elsewhere, "only_there.py");
  put(project, "only_here.py");
  world.state.bind(CHANNEL, elsewhere);
  await world.dispatch(typing("only", 1790000100.0, { viewHash: modal.hash }));
  await world.settle();
  assert.deepEqual(modal.rows(), ["only_here.py"]);
});

test(
  "a slow update is followed by the newest text and never by an older one",
  GIT_LAYOUT,
  async (t) => {
    // Two keystrokes whose payloads carry the same hash: the first update is slow, the second
    // waits behind it and lands after it, and the view's last word is the newest text. No update
    // is rejected: none carries a hash.
    const world = openWorld(t);
    const project = projectOf(world);
    const modal = new ModalSlack(world.slack);
    await inAThread(world);
    threeFiles(project);
    const gate = new AsyncEvent();
    world.slack.gate = gate;
    world.slack.gateMethod = "views.update";
    const shown = modal.hash;
    await world.dispatch(typing("one", 1790000100.0, { viewHash: shown }));
    assert.ok(world.slack.gated.isSet());
    await world.dispatch(typing("onetwot", 1790000100.5, { viewHash: shown }));
    await world.settle();
    assert.equal(modal.calls.length, 0); // the first is still in flight
    gate.set();
    await world.settle();
    assert.deepEqual(
      modal.calls.map(([, view]) => modal.rows(view)),
      [["one.py", "onetwo.py", "onetwothree.py"], ["onetwothree.py"]],
    );
    assert.deepEqual(modal.rows(), ["onetwothree.py"]);
    assert.equal(modal.written.length, 2);
    assert.deepEqual(new Set(modal.calls.map(([sent]) => sent)), new Set([null]));
  },
);

test("an update waiting behind a newer keystroke is never made", GIT_LAYOUT, async (t) => {
  const watched = { listings: null as WatchedListings | null };
  const world = worldWith(t, (lookup) => {
    watched.listings = new WatchedListings(lookup);
    return watched.listings;
  });
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  threeFiles(project);
  const gate = new AsyncEvent();
  world.slack.gate = gate;
  world.slack.gateMethod = "views.update";
  const shown = modal.hash;
  await world.dispatch(typing("one", 1790000100.0, { viewHash: shown }));
  assert.ok(world.slack.gated.isSet());
  await world.dispatch(typing("onet", 1790000100.5, { viewHash: shown }));
  await world.dispatch(typing("onetwot", 1790000101.0, { viewHash: shown }));
  await world.settle();
  gate.set();
  await world.settle();
  const shownRows = modal.calls.map(([, view]) => modal.rows(view));
  // The rows of "onet" (two files) were never written, nor even looked for.
  assert.ok(!shownRows.some((rows) => rows.join() === "onetwo.py,onetwothree.py"));
  assert.deepEqual(modal.rows(), ["onetwothree.py"]);
  assert.equal(watched.listings?.listed.length, 2); // the first keystroke and the last
});

test("a keystroke that arrives after a newer one is dropped", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  threeFiles(project);
  await world.dispatch(typing("onetwot", 1790000101.0, { viewHash: modal.hash }));
  await world.settle();
  assert.deepEqual(modal.rows(), ["onetwothree.py"]);
  await world.dispatch(typing("one", 1790000100.0, { viewHash: modal.hash })); // older, delivered late
  await world.settle();
  assert.equal(modal.calls.length, 1);
  assert.deepEqual(modal.rows(), ["onetwothree.py"]);
});

test("a keystroke delivered twice is written once", GIT_LAYOUT, async (t) => {
  // Slack sends an interaction again when its acknowledgement was missed.
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  threeFiles(project);
  await world.dispatch(typing("onetwo", 1790000100.0, { viewHash: modal.hash }));
  await world.dispatch(typing("onetwo", 1790000100.0, { viewHash: modal.hash }));
  await world.settle();
  assert.equal(modal.calls.length, 1);
});

test(
  "an update that slack rejects for any other reason is dropped quietly",
  GIT_LAYOUT,
  async (t) => {
    const world = openWorld(t);
    projectOf(world);
    const modal = new ModalSlack(world.slack);
    await inAThread(world);
    world.slack.responses["views.update"] = { ok: false, error: "not_found" };
    await world.dispatch(typing("read", 1790000100.0, { viewHash: modal.hash }));
    await world.settle();
    assert.equal(calls(world, "views.update").length, 1);
    assert.deepEqual(world.ephemerals(), []);
  },
);

test("an interaction is dated by its action ts", () => {
  const now = () => Date.now() / 1000;
  assert.equal(actionKey({ action_ts: "1790000100.500000" }, now), 1790000100.5);
  const arrival = now();
  for (const action of [{}, { action_ts: null }, { action_ts: "soon" }]) {
    assert.ok(actionKey(action, now) >= arrival);
  }
});

test(
  "a keystroke that arrives after the modal was submitted updates nothing",
  GIT_LAYOUT,
  async (t) => {
    const world = openWorld(t);
    const project = projectOf(world);
    const modal = new ModalSlack(world.slack);
    await inAThread(world);
    put(project, "src/app.py");
    await world.dispatch(typing("src", 1790000100.0));
    await world.settle();
    assert.equal(modal.calls.length, 1);
    await world.dispatch(submitted("src/app.py"));
    await world.dispatch(typing("src/a", 1790000101.0)); // sent before the submit, delivered after
    await world.settle();
    assert.equal(modal.calls.length, 1); // no update of a view that is closed
  },
);

test("open shares the chosen file and closes the modal", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  new ModalSlack(world.slack);
  await inAThread(world);
  put(project, "src/app.py");
  const response = await world.dispatch(submitted("src/app.py"));
  assert.equal(response.status, 200);
  assert.ok(!("response_action" in (response.body ?? {})));
  const done = only(opened(world));
  assert.ok(done.thread_ts === THREAD && done.channel_id === CHANNEL);
  assert.equal(done.files[0].title, "src/app.py");
  assert.deepEqual(world.ephemerals(), []);
});

test(
  "open with no row chosen shows the error on the rows and shares nothing",
  GIT_LAYOUT,
  async (t) => {
    const world = openWorld(t);
    projectOf(world);
    await inAThread(world);
    const before = world.slack.apiCalls.length;
    const response = await world.dispatch(submitted(null, { rows: ["a.py"] }));
    const rowsBlock = shownChoiceId(
      modalView(OPEN_TARGET, "", ["a.py"]).blocks as unknown as Body[],
    );
    assert.deepEqual(response.body, {
      response_action: "errors",
      errors: { [rowsBlock]: texts.OPEN_NONE_CHOSEN },
    });
    assert.deepEqual(opened(world), []);
    // The answer is the first thing sent, and nothing is sent after it.
    assert.equal(world.slack.apiCalls.length, before);
  },
);

test(
  "open with no rows to choose from shows the error on the search field",
  GIT_LAYOUT,
  async (t) => {
    const world = openWorld(t);
    projectOf(world);
    await inAThread(world);
    const blocks = modalView(OPEN_TARGET, "zz", []).blocks as unknown as Body[]; // no radio group in the view
    const response = await world.dispatch(submitted(null, { blocks }));
    assert.deepEqual(response.body, {
      response_action: "errors",
      errors: { [QUERY_BLOCK]: texts.OPEN_NONE_CHOSEN },
    });
    assert.deepEqual(opened(world), []);
  },
);

test("a row chosen before the rows changed is never opened", GIT_LAYOUT, async (t) => {
  // Slack keeps the state of an input block whose ids do not change across an update, so the
  // Submit can carry a row the view no longer shows. Both ways it can: under the id the older
  // rows had, and under the id of the rows now shown with a value that is not among them.
  const world = openWorld(t);
  const project = projectOf(world);
  await inAThread(world);
  put(project, "old.py");
  put(project, "new.py");
  const now = ["new.py"];
  const olderId = shownChoiceId(
    modalView(OPEN_TARGET, "", ["old.py", "other.py"]).blocks as unknown as Body[],
  );
  const nowId = shownChoiceId(modalView(OPEN_TARGET, "", now).blocks as unknown as Body[]);
  assert.notEqual(olderId, nowId);
  for (const under of [olderId, nowId]) {
    const before = world.slack.apiCalls.length;
    const response = await world.dispatch(submitted("old.py", { rows: now, under }));
    assert.deepEqual(response.body, {
      response_action: "errors",
      errors: { [nowId]: texts.OPEN_NONE_CHOSEN },
    });
    assert.ok(opened(world).length === 0 && world.slack.apiCalls.length === before);
  }
  // The control: a row of the rows shown, chosen under their id, is opened.
  await world.dispatch(submitted("new.py", { rows: now }));
  assert.equal(opened(world).length, 1);
});

for (const [user, team] of ANYONE_ELSE) {
  test(`open by anyone else shares nothing [${user}-${team}]`, GIT_LAYOUT, async (t) => {
    const world = openWorld(t);
    const project = projectOf(world);
    await inAThread(world);
    put(project, "a.py");
    assert.equal((await world.dispatch(submitted("a.py", { user, team }))).status, 200);
    assert.ok(opened(world).length === 0 && world.slack.uploaded.length === 0);
    assert.deepEqual(world.ephemerals(), []);
    // With nothing chosen they get no error to read either: a plain acknowledgement.
    const unanswered = await world.dispatch(submitted(null, { user, team }));
    assert.ok(!("response_action" in (unanswered.body ?? {})));
  });
}

test("open in a modal with metadata that is not ours shares nothing", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  await inAThread(world);
  put(project, "a.py");
  for (const metadata of ["", "not json", '{"c": "C000CHAN"}']) {
    assert.equal((await world.dispatch(submitted("a.py", { metadata }))).status, 200);
  }
  assert.ok(opened(world).length === 0 && world.slack.uploaded.length === 0);
});

test("open in a thread that holds no session shares nothing", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  put(project, "a.py");
  await world.dispatch(submitted("a.py"));
  assert.deepEqual(opened(world), []);
  assert.deepEqual(world.ephemerals(), [texts.NOT_A_SESSION]);
});

test("open in a refused channel shares nothing", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  await inAThread(world);
  put(project, "a.py");
  world.slack.responses["conversations.members"] = { ok: true, members: [OWNER, BOT, STRANGER] };
  await world.dispatch(submitted("a.py"));
  assert.deepEqual(opened(world), []);
  assert.deepEqual(world.ephemerals(), [
    fill(texts.CHANNEL_REFUSED, { reason: texts.REASON_MEMBERS }),
  ]);
});

for (const value of ["../outside.txt", "link.txt", "dir-link/secret.txt", "/etc/hosts", "docs"]) {
  test(`a value that leaves the folder or is no file is refused [${value}]`, {
    ...GIT_LAYOUT,
    ...SYMLINKS,
  }, async (t) => {
    const world = openWorld(t);
    const project = projectOf(world);
    await inAThread(world);
    // Python's `tmp_path` held the secret beside the root: `../outside.txt` and
    // `dir-link/secret.txt` then named nothing at all. Here both name a file that exists, so
    // that only the rule keeps them from being shared.
    const secret = put(world.tmpPath, "outside.txt");
    put(world.root, "outside.txt");
    put(world.tmpPath, "secret.txt");
    symlinkSync(secret, join(project, "link.txt"));
    symlinkSync(world.tmpPath, join(project, "dir-link"));
    mkdirSync(join(project, "docs"));
    await world.dispatch(submitted(value));
    assert.ok(opened(world).length === 0 && world.slack.uploaded.length === 0);
    assert.deepEqual(world.ephemerals(), [fill(texts.OPEN_NOT_A_FILE, { path: value })]);
  });
}

test("open resolves in the folder of the thread s own session", GIT_LAYOUT, async (t) => {
  // D5: the channel was bound to another folder after the thread began.
  const world = openWorld(t);
  const project = projectOf(world);
  await inAThread(world);
  const elsewhere = join(world.root, "elsewhere");
  put(elsewhere, "only_there.py");
  put(project, "only_here.py");
  world.state.bind(CHANNEL, elsewhere);
  await world.dispatch(submitted("only_there.py"));
  assert.deepEqual(opened(world), []);
  await world.dispatch(submitted("only_here.py"));
  assert.equal(opened(world).length, 1);
});

test("the message of open stays so another file can be chosen", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  new ModalSlack(world.slack);
  await inAThread(world);
  put(project, "a.py");
  put(project, "b.py");
  await askedOpen(world, "!open");
  const post = only(pickerPosts(world));
  await world.dispatch(submitted("a.py"));
  await world.dispatch(submitted("b.py"));
  assert.equal(opened(world).length, 2);
  // Left as it was: neither deleted nor rewritten (the session's setup message is rewritten).
  assert.deepEqual(calls(world, "chat.delete"), []);
  const rewritten = new Set(calls(world, "chat.update").map((call) => call.ts));
  assert.ok(!rewritten.has(world.slack.postedTs.at(-1)));
  assert.ok(post.blocks);
});

test("open with the path of a file shares it into the thread", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  await inAThread(world);
  put(project, "docs/guide.md", "# Guide\n");
  const posts = calls(world, "chat.postMessage").length;
  await askedOpen(world, "!open docs/guide.md");
  const asked = only(calls(world, "files.getUploadURLExternal"));
  assert.ok(asked.filename === "guide.md" && asked.length === Buffer.byteLength("# Guide\n"));
  assert.deepEqual(
    world.slack.uploaded.map((upload) => upload.data),
    [Buffer.from("# Guide\n")],
  );
  const done = only(opened(world));
  assert.ok(done.channel_id === CHANNEL && done.thread_ts === THREAD);
  assert.deepEqual(done.files, [{ id: "F000FILE", title: "docs/guide.md" }]);
  // No line of its own on success: the file is the answer.
  assert.equal(calls(world, "chat.postMessage").length, posts);
  assert.deepEqual(world.ephemerals(), []);
});

test("a path needs no git", async (t) => {
  const world = openWorld(t);
  await inAThread(world);
  put(join(world.root, "app"), "plain.txt");
  await askedOpen(world, "!open plain.txt");
  assert.equal(opened(world).length, 1);
});

test("a name that matches one file opens it", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  await inAThread(world);
  put(project, "src/Parser.py");
  put(project, "src/lexer.py");
  await askedOpen(world, "!open PARS");
  const done = only(opened(world));
  assert.equal(done.files[0].title, "src/Parser.py");
});

test(
  "a file made after a name matched nothing is found by the next search",
  GIT_LAYOUT,
  async (t) => {
    // A kept listing is never the reason for "no match": it is made again first.
    const world = openWorld(t);
    const project = projectOf(world);
    await inAThread(world);
    await askedOpen(world, "!open zzz");
    assert.deepEqual(world.ephemerals(), [fill(texts.OPEN_NO_MATCH, { words: "zzz" })]);
    put(project, "src/zzz-made-since.py");
    await askedOpen(world, "!open zzz");
    const done = only(opened(world));
    assert.equal(done.files[0].title, "src/zzz-made-since.py");
  },
);

test("typing finds a file made after the search matched nothing", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  await world.dispatch(typing("qqq", 1790000100.0));
  await world.settle();
  assert.deepEqual(modal.rows(), []);
  assert.deepEqual(modal.labels(), ["0 files match"]);
  put(project, "qqq-made-since.py");
  await world.dispatch(typing("qqq-", 1790000101.0));
  await world.settle();
  assert.deepEqual(modal.rows(), ["qqq-made-since.py"]);
});

test("a name that matches nothing in a listing that was cut says so", async (t) => {
  const world = worldWith(t, (lookup) => new CutListings(lookup, ["a/other.py"]));
  await inAThread(world);
  await askedOpen(world, "!open nothing-like-it");
  assert.deepEqual(opened(world), []);
  assert.deepEqual(world.ephemerals(), [
    fill(texts.OPEN_NO_MATCH_PARTIAL, { words: "nothing-like-it" }),
  ]);
});

test("the only match of a listing that was cut is not opened on its own", async (t) => {
  const world = worldWith(t, (lookup) => new CutListings(lookup, ["a/one.py"]));
  await inAThread(world);
  put(join(world.root, "app"), "a/one.py");
  await askedOpen(world, "!open one");
  assert.deepEqual(opened(world), []);
  const post = only(pickerPosts(world));
  const [section, actions, context] = post.blocks as [Body, Body, Body];
  assert.equal(section.text.text, fill(texts.OPEN_MATCHES_ONE, { words: "one" }));
  assert.equal(context.elements[0].text, texts.OPEN_PARTIAL);
  assert.equal(actions.elements[0].value, "one");
});

test("a listing that was cut is said in the modal too", async (t) => {
  const world = worldWith(t, (lookup) => new CutListings(lookup, ["a/one.py"]));
  const modal = new ModalSlack(world.slack);
  await inAThread(world);
  put(join(world.root, "app"), "a/one.py");
  await world.dispatch(typing("zzz", 1790000100.0));
  await world.settle();
  assert.deepEqual(modal.labels(), ["0 files match", texts.OPEN_PARTIAL]);
  await world.dispatch(typing("one", 1790000101.0));
  await world.settle();
  assert.deepEqual(modal.rows(), ["a/one.py"]);
  assert.deepEqual(modal.labels(), ["1 file matches", texts.OPEN_PARTIAL]);
});

test("a name that matches nothing says so", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  projectOf(world);
  await inAThread(world);
  await askedOpen(world, "!open nothing-like-it");
  assert.deepEqual(opened(world), []);
  assert.deepEqual(world.ephemerals(), [fill(texts.OPEN_NO_MATCH, { words: "nothing-like-it" })]);
});

test("a file over one megabyte is refused", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  await inAThread(world);
  writeFileSync(join(project, "big.log"), Buffer.alloc((1 << 20) + 1, "x"));
  await askedOpen(world, "!open big.log");
  assert.deepEqual(calls(world, "files.getUploadURLExternal"), []);
  assert.deepEqual(world.ephemerals(), [fill(texts.OPEN_TOO_LARGE, { path: "big.log" })]);
});

test("an empty file is refused before anything is uploaded", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  await inAThread(world);
  put(project, "empty.txt", "");
  await askedOpen(world, "!open empty.txt");
  assert.deepEqual(calls(world, "files.getUploadURLExternal"), []);
  assert.deepEqual(world.slack.uploaded, []);
  assert.deepEqual(world.ephemerals(), [fill(texts.OPEN_EMPTY, { path: "empty.txt" })]);
  // Chosen in the modal it is the same file, with the same answer.
  await world.dispatch(submitted("empty.txt"));
  assert.deepEqual(world.slack.uploaded, []);
  assert.deepEqual(
    world.ephemerals(),
    Array(2).fill(fill(texts.OPEN_EMPTY, { path: "empty.txt" })),
  );
});

test("without the files write scope the owner is told what to do", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  await inAThread(world);
  put(project, "a.md");
  world.slack.responses["files.getUploadURLExternal"] = { ok: false, error: "missing_scope" };
  await askedOpen(world, "!open a.md");
  assert.deepEqual(world.ephemerals(), [texts.OPEN_NO_SCOPE]);
  assert.ok(texts.OPEN_NO_SCOPE.includes("files:write"));
  assert.ok(texts.OPEN_NO_SCOPE.includes("slack-app-manifest.json"));
});

test("another slack failure names its code and no file content", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  const project = projectOf(world);
  await inAThread(world);
  put(project, "a.md", "secret words\n");
  world.slack.responses["files.completeUploadExternal"] = { ok: false, error: "ratelimited" };
  await askedOpen(world, "!open a.md");
  assert.deepEqual(world.ephemerals(), [
    fill(texts.OPEN_FAILED, { path: "a.md", error: "ratelimited" }),
  ]);
});

test("a word outside a session s thread is answered as bypass is", GIT_LAYOUT, async (t) => {
  const world = openWorld(t);
  projectOf(world);
  await world.dispatch(message("!open"));
  await world.dispatch(reply("!open", OTHER_THREAD)); // a thread that holds no session
  assert.deepEqual(said(world), [texts.OPEN_TOP_LEVEL, texts.OPEN_TOP_LEVEL]);
  assert.deepEqual(pickerPosts(world), []);
  assert.deepEqual(opened(world), []);
});

test("a name is looked up in a folder with no repository", async (t) => {
  const world = openWorld(t);
  await inAThread(world);
  const plain = join(world.root, "app");
  put(plain, "docs/Guide.md");
  put(plain, "other.txt");
  await askedOpen(world, "!open guide");
  const done = only(opened(world));
  assert.equal(done.files[0].title, "docs/Guide.md");
  assert.deepEqual(world.ephemerals(), []);
});
