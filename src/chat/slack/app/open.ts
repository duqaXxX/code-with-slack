/**
 * `!open`: a file of a session's folder shared into its thread, by name or through the picker's
 * modal. The logic is `openfile/`; these are its handlers. Port of `thread_folder`, `open_word`,
 * `found_files`, `open_file`, `update_modal`, `open_modal`, `on_open_choose`, `on_open_query` and
 * `on_open_submit` of `slack_app.py`.
 *
 * Python made a task of the listing a click starts and cancelled it when the click was done.
 * Here the listing is a promise that began with the click and a signal that gives it up: a walk
 * or a git call in flight is never cut, the signal only stops what would follow it.
 */
import type { ThreadSession } from "../../../core/sessions/session.ts";
import * as texts from "../../../core/texts.ts";
import { fill } from "../../../core/texts.ts";
import { changedIn } from "../openfile/changed.ts";
import type { Found } from "../openfile/listing.ts";
import {
  BadTarget,
  choiceBlock,
  chosenIn,
  ModalUpdates,
  matchesBlocks,
  modalView,
  OPEN_WAIT,
  pickerBlocks,
  QUERY_BLOCK,
  QUERY_LIMIT,
  ROW_LIMIT,
  Target,
  typedIn,
} from "../openfile/modal.ts";
import {
  NotAFile,
  newestFirst,
  readOpenable,
  regularFiles,
  TooLarge,
  uploadFile,
} from "../openfile/read.ts";
import { strip, take } from "../reply/chars.ts";
import { describe } from "../reply/errors.ts";
import { shownAsWritten } from "../reply/escape.ts";
import { Cancelled } from "../reply/tasks.ts";
import { type Answers, type AppParts, errorName, isCancelled, logger } from "./answers.ts";
import { interactionActor, isOwner } from "./guards.ts";
import {
  type Ack,
  actionKey,
  clickThread,
  firstAction,
  type Payload,
  record,
  text,
} from "./wire.ts";

/** The words of a search: what was typed, stripped as Python strips, cut at `QUERY_LIMIT` characters. */
function searched(typed: string): string {
  return take(strip(typed), QUERY_LIMIT);
}

/** A listing a click started: its answer once it has one. */
interface Finding {
  readonly promise: Promise<Found>;
  found: Found | null;
}

export class Open {
  private readonly parts: AppParts;
  private readonly answers: Answers;
  /** The open picker modals, so that an older update of one never overwrites a newer one. */
  private readonly modals = new ModalUpdates();

  constructor(parts: AppParts, answers: Answers) {
    this.parts = parts;
    this.answers = answers;
  }

  /**
   * The folder of the session that owns a thread, from its entry; null when the thread is no
   * session (or is being deleted). What a click or a typed query names is resolved here and
   * never against the channel's folder, which a later `!bind` may have moved.
   */
  threadFolder(channel: string, threadTs: string): string | null {
    const { state, sessions } = this.parts;
    const thread = state.thread(channel, threadTs);
    if (thread === null || sessions.held(channel, threadTs)) return null;
    return thread.directory;
  }

  /**
   * `!open` typed in a session's thread: a picker alone; with words, the file they name as a
   * path, else the file whose name contains them, else the files that do. A file is opened on
   * its own only when it is the one match of a listing that is complete.
   */
  async openWord(
    channel: string,
    threadTs: string,
    session: ThreadSession,
    words: string,
  ): Promise<void> {
    const folder = session.directory;
    if (!words) {
      await this.answers.deliver(channel, threadTs, texts.OPEN_FALLBACK, pickerBlocks(), {
        ephemeral: false,
      });
      return;
    }
    if ((await regularFiles(folder, [words])).length > 0) {
      await this.openFile(channel, threadTs, folder, words);
      return;
    }
    const found = await this.foundFiles(folder, threadTs, words);
    const [only] = found.paths;
    if (only === undefined) {
      const shown = shownAsWritten(words);
      const noMatch = found.complete ? texts.OPEN_NO_MATCH : texts.OPEN_NO_MATCH_PARTIAL;
      await this.answers.tellOwner(channel, threadTs, fill(noMatch, { words: shown }));
    } else if (found.paths.length === 1 && found.complete) {
      await this.openFile(channel, threadTs, folder, only);
    } else {
      await this.answers.deliver(
        channel,
        threadTs,
        texts.OPEN_FALLBACK,
        matchesBlocks(words, found.paths, found.complete),
        { ephemeral: false },
      );
    }
  }

  /**
   * What the picker's rows are chosen from, as paths from `folder`: with no `words`, the files
   * changed in the session, newest first (the union over the folder's repositories, none when
   * there is no repository or nothing changed); else the files whose path contains them, in
   * `rank`'s order, of which at most `limit` are checked on disk (`Listings.search`). Never
   * rejects but for the signal: a listing that fails is empty and incomplete.
   */
  async foundFiles(
    folder: string,
    threadTs: string,
    words: string,
    options: { readonly limit?: number; readonly signal?: AbortSignal } = {},
  ): Promise<Found> {
    const { listings } = this.parts;
    const { limit, signal } = options;
    try {
      if (!words) {
        const repositories = await listings.repositories(folder, { ...(signal && { signal }) });
        signal?.throwIfAborted();
        const names = await changedIn(folder, repositories, threadTs);
        signal?.throwIfAborted();
        const paths = await newestFirst(folder, names);
        return { paths, count: paths.length, complete: true };
      }
      return await listings.search(folder, words, {
        ...(limit !== undefined && { limit }),
        ...(signal && { signal }),
      });
    } catch (error) {
      if (signal?.aborted || isCancelled(error)) throw error;
      logger.warning(`could not list files for a search: ${errorName(error)}`);
      return { paths: [], count: 0, complete: false };
    }
  }

  /**
   * Share the file `relative` names under `folder` into the thread, which Slack shows in its own
   * viewer; no line of ours on success. `relative` is untrusted.
   */
  async openFile(
    channel: string,
    threadTs: string,
    folder: string,
    relative: string,
  ): Promise<void> {
    const { slack, sessions } = this.parts;
    const shown = shownAsWritten(relative);
    let content: Buffer;
    try {
      content = await readOpenable(folder, relative);
    } catch (error) {
      if (error instanceof NotAFile) {
        await this.answers.tellOwner(
          channel,
          threadTs,
          fill(texts.OPEN_NOT_A_FILE, { path: shown }),
        );
        return;
      }
      if (error instanceof TooLarge) {
        await this.answers.tellOwner(
          channel,
          threadTs,
          fill(texts.OPEN_TOO_LARGE, { path: shown }),
        );
        return;
      }
      throw error;
    }
    if (content.length === 0) {
      // Refused here, not left to whatever Slack answers to an upload of length 0.
      await this.answers.tellOwner(channel, threadTs, fill(texts.OPEN_EMPTY, { path: shown }));
      return;
    }
    try {
      await uploadFile(slack, channel, threadTs, content, relative);
    } catch (error) {
      if (isCancelled(error)) throw error;
      const code = describe(error);
      // Slack's own code, never the file or its path.
      logger.warning(`could not share a file in ${channel}: ${code}`);
      let told: string;
      if (code === "missing_scope") told = texts.OPEN_NO_SCOPE;
      else if (code === "snippet_too_large") told = fill(texts.OPEN_TOO_LARGE, { path: shown });
      else told = fill(texts.OPEN_FAILED, { path: shown, error: code });
      await this.answers.tellOwner(channel, threadTs, told);
      return;
    }
    // Slack clears a thread's status line when the app shares into it.
    sessions.wrote(channel, threadTs);
  }

  /**
   * Put the rows for `words` into the open modal `viewId`, unless a newer update has been taken
   * meanwhile (`ModalUpdates`): `key` orders them. No `hash` goes with it: this daemon is the
   * only writer of its modals, and `modals` makes the newest update the last.
   */
  private async updateModal(
    viewId: string,
    key: number,
    target: Target,
    words: string,
    found: () => Promise<Found>,
  ): Promise<void> {
    const { slack } = this.parts;
    // (sync) Taken as the event comes in: the characters of one search are handled side by
    // side, and what orders their updates is the key each one claimed, not when it finishes.
    if (!this.modals.claim(viewId, key)) return;
    await this.modals.lock(viewId).run(async () => {
      if (!this.modals.current(viewId, key)) return;
      const rows = await found();
      if (!this.modals.current(viewId, key)) return;
      const view = modalView(target, words, rows.paths, {
        count: rows.count,
        complete: rows.complete,
      });
      try {
        await slack.views.update({ view_id: viewId, view });
      } catch (error) {
        if (isCancelled(error)) throw error;
        logger.warning(`could not update the file picker: ${describe(error)}`);
      }
    });
  }

  /**
   * `finding`'s answer when it has one within `seconds` of the clock, else null: Python's
   * `asyncio.wait` with a timeout, which also takes a listing that had already ended when no
   * time was left.
   */
  private async awaited(finding: Finding, seconds: number): Promise<Found | null> {
    if (seconds > 0 && finding.found === null) {
      const waited = new AbortController();
      await Promise.race([
        finding.promise.then(
          () => {},
          () => {},
        ),
        this.parts.clock.sleep(seconds, waited.signal).catch(() => {}),
      ]);
      waited.abort(new Cancelled());
    }
    // One turn of the event loop: a listing that ended as the time ran out is still taken.
    await new Promise<void>((resolve) => setImmediate(resolve));
    return finding.found;
  }

  /**
   * Open the picker's modal on the click `triggerId` came with, which lives 3 seconds: with the
   * rows when `finding` has them by OPEN_WAIT after the click came in (`entered`, on the
   * handlers' clock), else without them and filled by an update.
   */
  private async openModal(
    channel: string,
    threadTs: string,
    finding: Finding,
    triggerId: string | null,
    words: string,
    entered: number,
  ): Promise<void> {
    const { slack, clock } = this.parts;
    const target = new Target(channel, threadTs);
    const left = OPEN_WAIT - (clock.time() - entered);
    const found = await this.awaited(finding, Math.max(left, 0));
    const first =
      found === null
        ? modalView(target, words, null, { opening: true })
        : modalView(target, words, found.paths, {
            count: found.count,
            complete: found.complete,
            opening: true,
          });
    let viewId: unknown;
    try {
      if (triggerId === null) throw new TypeError("the click carries no trigger_id");
      const opened = await slack.views.open({ trigger_id: triggerId, view: first });
      viewId = opened.view?.id;
    } catch (error) {
      if (isCancelled(error)) throw error;
      // an expired trigger_id, say
      const message = fill(texts.OPEN_FORM_NOT_OPENED, { error: describe(error) });
      await this.answers.tellOwner(channel, threadTs, message);
      return;
    }
    if (found === null && typeof viewId === "string") {
      // Key 0: any keystroke typed meanwhile is newer.
      await this.updateModal(viewId, 0.0, target, words, () => finding.promise);
    }
  }

  /**
   * The `Choose a file` button, of `!open` or of its several matches: opens the modal. The
   * click's `trigger_id` lives 3 seconds, and the checks, the wait for the rows and `views.open`
   * all come out of them: the listing starts at once, beside the channel check and counted from
   * here.
   */
  onOpenChoose = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    const { identity, clock } = this.parts;
    const entered = clock.time();
    const [user, team] = interactionActor(body);
    const channel = text(record(body.channel).id);
    const threadTs = clickThread(body);
    let words = "";
    let finding: Finding | null = null;
    const given = new AbortController();
    // What is local comes first: only the owner's click, in a session's thread, lists anything.
    if (channel && isOwner(identity, user, team)) {
      // The words only fill the field: what is listed is what they match in this thread's
      // folder.
      const typed = firstAction(body).value;
      words = typeof typed === "string" ? searched(typed) : "";
      const folder = this.threadFolder(channel, threadTs);
      if (folder !== null) {
        const listing: Finding = {
          promise: this.foundFiles(folder, threadTs, words, {
            limit: ROW_LIMIT,
            signal: given.signal,
          }),
          found: null,
        };
        listing.promise.then(
          (found) => {
            listing.found = found;
          },
          () => {}, // given up: nobody reads it
        );
        finding = listing;
      }
    }
    try {
      if (!(await this.answers.admitted(user, team, channel, threadTs)) || channel === null) return;
      if (finding === null) {
        await this.answers.tellOwner(channel, threadTs, texts.NOT_A_SESSION);
        return;
      }
      const listed = finding;
      await this.answers.replyOnFailure(
        channel,
        threadTs,
        () => this.openModal(channel, threadTs, listed, text(body.trigger_id), words, entered),
        (told) => this.answers.tellOwner(channel, threadTs, told),
      );
    } finally {
      given.abort(new Cancelled());
    }
  };

  /**
   * A character typed in the modal's search field: its rows follow what is typed. Checked for
   * the owner and the workspace alone, with no call to Slack for the channel: each character
   * comes here, and the rows reach only the owner's own modal.
   */
  onOpenQuery = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    const { identity, now } = this.parts;
    const [user, team] = interactionActor(body);
    if (!isOwner(identity, user, team)) {
      logger.info("ignored an inbound event from someone other than the owner");
      return;
    }
    const view = record(body.view);
    let target: Target;
    try {
      target = Target.load(view.private_metadata);
    } catch (error) {
      if (error instanceof BadTarget) return;
      throw error;
    }
    const viewId = view.id;
    const folder = this.threadFolder(target.channel, target.threadTs);
    if (folder === null || typeof viewId !== "string") return;
    const action = firstAction(body);
    const typed =
      typeof action.value === "string" ? action.value : typedIn(record(view.state).values);
    const words = searched(typed);
    await this.updateModal(viewId, actionKey(action, now), target, words, () =>
      this.foundFiles(folder, target.threadTs, words, { limit: ROW_LIMIT }),
    );
  };

  /**
   * Open, in the picker's modal: the row chosen is shared into the thread and the modal closes.
   * The answer to Slack comes first and makes no call to it; the value and the metadata are
   * untrusted, and the file is read from the folder of the thread's own session.
   */
  onOpenSubmit = async (ack: Ack, body: Payload): Promise<void> => {
    const { identity } = this.parts;
    const [user, team] = interactionActor(body);
    const view = record(body.view);
    let target: Target;
    try {
      target = Target.load(view.private_metadata);
    } catch (error) {
      if (!(error instanceof BadTarget)) throw error;
      await ack();
      return;
    }
    if (!isOwner(identity, user, team)) {
      await ack();
      logger.info("ignored an inbound event from someone other than the owner");
      return;
    }
    const relative = chosenIn(view);
    if (relative === null) {
      // The error goes on the rows the view shows, whatever the state holds. With no row
      // there is no such block to carry it: the search field does.
      const rows = choiceBlock(view);
      const block =
        rows !== null && typeof rows.block_id === "string" ? rows.block_id : QUERY_BLOCK;
      await ack({ response_action: "errors", errors: { [block]: texts.OPEN_NONE_CHOSEN } });
      return;
    }
    await ack();
    if (typeof view.id === "string") this.modals.forget(view.id);
    const { channel, threadTs } = target;
    if (!(await this.answers.admitted(user, team, channel, threadTs))) return;
    const folder = this.threadFolder(channel, threadTs);
    if (folder === null) {
      await this.answers.tellOwner(channel, threadTs, texts.NOT_A_SESSION);
      return;
    }
    await this.answers.replyOnFailure(
      channel,
      threadTs,
      () => this.openFile(channel, threadTs, folder, relative),
      (told) => this.answers.tellOwner(channel, threadTs, told),
    );
  };
}
