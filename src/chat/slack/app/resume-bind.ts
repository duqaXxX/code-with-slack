/**
 * `!bind` and `!resume`, typed or clicked: which folder a channel's next thread starts in, and
 * which stored session a thread continues. Port of `bind_to`, `announce_bind`, `folder_named`,
 * `list_folders`, `on_bind`, `bind_clicked`, `handle_resume`, `resume_into_thread`, `on_resume`,
 * `resume_clicked` and `show_resumed` of `slack_app.py`.
 */
import type { ListedSession } from "../../../agent/seam.ts";
import { oneLine } from "../../../core/reply/words.ts";
import { resolveDirectory } from "../../../core/sessions/directory.ts";
import type { DirectoryUnavailable } from "../../../core/sessions/errors.ts";
import * as texts from "../../../core/texts.ts";
import { fill } from "../../../core/texts.ts";
import { bindBlocks } from "../bind.ts";
import { compareCodePoints } from "../openfile/paths.ts";
import { contextBlock, noticeText } from "../reply/blocks.ts";
import { describe } from "../reply/errors.ts";
import { markdownEscape, mrkdwnEscape } from "../reply/escape.ts";
import { matching, parseResumeValue, resumeBlocks, TITLE_LIMIT } from "../resume.ts";
import { type Answers, type AppParts, isCancelled, logger } from "./answers.ts";
import { interactionActor } from "./guards.ts";
import {
  type Ack,
  clickThread,
  firstAction,
  messageTs,
  type Payload,
  record,
  str,
  text,
} from "./wire.ts";

/**
 * The answer to a bind: what keeps a session from starting in the folder, if anything
 * (`BIND_UNAVAILABLE` keeps priority: what would stop every session there matters more than
 * where an existing thread's session lives), else the old folders existing threads keep working
 * in, if any.
 */
export function boundText(
  directory: string,
  unavailable: DirectoryUnavailable | null,
  oldFolders: readonly string[],
): string {
  if (unavailable !== null) {
    return fill(texts.BIND_UNAVAILABLE, {
      directory: mrkdwnEscape(directory),
      reason: unavailable.message,
    });
  }
  if (oldFolders.length === 0) {
    return fill(texts.BIND_OK, { directory: mrkdwnEscape(directory) });
  }
  const old = oldFolders.map((folder) => `\`${mrkdwnEscape(folder)}\``).join(", ");
  return fill(texts.BIND_OK_ELSEWHERE, { directory: mrkdwnEscape(directory), old });
}

export class ResumeBind {
  private readonly parts: AppParts;
  private readonly answers: Answers;

  constructor(parts: AppParts, answers: Answers) {
    this.parts = parts;
    this.answers = answers;
  }

  /** `!bind <folder>` typed in the channel. */
  async bindTo(channel: string, path: string): Promise<void> {
    const directory = await this.folderNamed(channel, path);
    if (directory === null) return;
    if (!(await this.parts.sessions.bind(channel, directory))) {
      await this.answers.inChannel(channel, texts.BIND_BUSY);
      return;
    }
    await this.announceBind(channel, directory);
  }

  // Thread entries keep their own folder either side of the bind (`StateStore.bind`), so the
  // old folders can be read now: those of the channel's threads that differ from the new one.
  private async announceBind(channel: string, directory: string): Promise<void> {
    const { sessions, state } = this.parts;
    const unavailable = await sessions.unavailable(directory);
    const record = state.channel(channel);
    const threads = record === null ? [] : [...record.threads.values()];
    const oldFolders = [
      ...new Set(threads.map((thread) => thread.directory).filter((old) => old !== directory)),
    ].sort(compareCodePoints);
    await this.answers.inChannel(channel, boundText(directory, unavailable, oldFolders));
  }

  /** The folder a typed path or a button names, inside the allowed root; null, told, otherwise. */
  private async folderNamed(channel: string, path: string): Promise<string | null> {
    const root = this.parts.config.allowedRoot;
    const directory = resolveDirectory(path, root);
    if (directory === null) {
      await this.answers.inChannel(
        channel,
        fill(texts.BIND_OUTSIDE, { path: mrkdwnEscape(path), root: mrkdwnEscape(root) }),
      );
    }
    return directory;
  }

  /** `!bind` alone: the folders a session can start in, each with its button. */
  async listFolders(channel: string): Promise<void> {
    const { config, sessions, state } = this.parts;
    const root = config.allowedRoot;
    const record = state.channel(channel);
    const folders = await sessions.foldersIn(root);
    const blocks = bindBlocks(root, folders, record === null ? null : record.directory);
    await this.answers.deliver(
      channel,
      null,
      fill(folders.length > 0 ? texts.BIND_LIST : texts.BIND_EMPTY, { root }),
      blocks,
      { ephemeral: false },
    );
  }

  /** A Bind button of the folder list. */
  onBind = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    const [user, team] = interactionActor(body);
    const channel = text(record(body.channel).id);
    const threadTs = clickThread(body);
    if (!(await this.answers.admitted(user, team, channel, threadTs)) || channel === null) return;
    await this.answers.replyOnFailure(
      channel,
      threadTs,
      () => this.bindClicked(channel, body),
      (told) => this.answers.inChannel(channel, told),
    );
  };

  private async bindClicked(channel: string, body: Payload): Promise<void> {
    const { sessions, state } = this.parts;
    const threadTs = clickThread(body);
    // The button is not trusted: its folder goes through the same check as a typed `!bind`.
    const directory = await this.folderNamed(channel, str(firstAction(body).value));
    if (directory === null) return;
    const record_ = state.channel(channel);
    if (record_ !== null && record_.directory === directory) {
      const shown = mrkdwnEscape(directory);
      await this.answers.inChannel(channel, fill(texts.BIND_ALREADY, { directory: shown }));
      return;
    }
    // A list can be old: unlike a typed `!bind`, a click never ends work in flight either.
    if (!(await sessions.bind(channel, directory))) {
      await this.answers.inChannel(channel, texts.BIND_BUSY);
      return;
    }
    await this.announceBind(channel, directory);
    await this.answers.removeRequest(channel, threadTs, messageTs(body));
  }

  /**
   * `!resume` typed in the channel (`threadTs` is the thread of the owner's word: the message
   * itself when top-level). Every answer is a normal post in the channel; the session chosen, by
   * the list or by name, lives in that thread.
   */
  async handleResume(channel: string, threadTs: string, target: string): Promise<void> {
    const { config, sessions, state, now } = this.parts;
    const record_ = state.channel(channel);
    if (record_ === null) {
      await this.answers.inChannel(channel, fill(texts.UNBOUND, { root: config.allowedRoot }));
      return;
    }
    const directory = record_.directory;
    const stored = await sessions.sessionsIn(directory);
    if (!target) {
      // A session a thread already holds cannot be resumed: it takes no row, so the rows are
      // the ones the owner can pick (issue #69). Only the list shows dates, and dating reads
      // files: the sessions left out are not dated.
      const free = stored.filter((session) => state.holder(session.id) === null);
      const blocks = resumeBlocks(
        directory,
        await sessions.dated(directory, free),
        stored.length - free.length,
        new Date(now() * 1000),
        threadTs,
      );
      await this.answers.deliver(channel, null, fill(texts.RESUME_LIST, { directory }), blocks, {
        ephemeral: false,
      });
      return;
    }
    const found = matching(stored, target);
    const [only] = found;
    if (found.length !== 1 || only === undefined) {
      const template = found.length > 0 ? texts.RESUME_AMBIGUOUS : texts.RESUME_NONE;
      await this.answers.inChannel(
        channel,
        fill(template, { directory: mrkdwnEscape(directory), target: mrkdwnEscape(target) }),
      );
      return;
    }
    await this.resumeIntoThread(channel, threadTs, directory, only);
  }

  /**
   * Point the thread rooted at `threadTs` at `chosen`, a session read from `directory`; false,
   * after telling the owner why in the channel, when nothing changed: that thread already holds
   * a session (a resume is never a swap), `chosen` is already held by some other thread (one
   * session lives in one thread), or the channel was bound to another folder while `chosen` was
   * read from `directory`. Every check runs with no `await` before the `resume` they guard, so
   * nothing can change between the checks and the call they protect. `listTs`: the picker a
   * click came from, removed once the confirmation is posted or has failed, so buttons never
   * outlive a resume.
   */
  async resumeIntoThread(
    channel: string,
    threadTs: string,
    directory: string,
    chosen: ListedSession,
    listTs: string | null = null,
  ): Promise<boolean> {
    const { sessions, state } = this.parts;
    // (sync) From here to `sessions.resume`, which opens the thread's entry inside the call:
    // each refusal below leaves through its own `await`, and nothing is awaited on the way
    // to the resume itself.
    // A thread being deleted still holds its session's entry, and hands out no session.
    if (sessions.get(channel, threadTs) !== null || sessions.held(channel, threadTs)) {
      await this.answers.inChannel(channel, texts.RESUME_HELD);
      return false;
    }
    const holder = state.holder(chosen.id);
    if (holder !== null) {
      const link = await this.answers.threadMrkdwnLink(holder[0], holder[1], "Session");
      await this.answers.inChannel(channel, fill(texts.RESUME_ELSEWHERE, { link }));
      return false;
    }
    const record_ = state.channel(channel);
    if (record_ === null || record_.directory !== directory) {
      await this.answers.inChannel(channel, texts.RESUME_GONE);
      return false;
    }
    const session = await sessions.resume(channel, threadTs, chosen.id);
    // just confirmed the channel is bound to `directory`
    if (session === null) throw new Error("a bound channel resumed no session");
    // A markdown block, not mrkdwn: the title is escaped so it cannot close or open the bold.
    const title = markdownEscape(oneLine(chosen.title, TITLE_LIMIT)) || chosen.id;
    let confirmed = false;
    try {
      await this.answers.say(channel, threadTs, fill(texts.RESUME_OK, { title }));
      confirmed = true;
    } finally {
      // Independent of the confirmation: a failing one still takes the list's buttons away,
      // and a failure there (swallowed in `showResumed`) never blocks it. With no
      // confirmation in the thread the list is kept, rewritten, as the record.
      await this.showResumed(channel, threadTs, listTs, chosen, { remove: confirmed });
    }
    return true;
  }

  /** A Resume button of the session list. */
  onResume = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    const [user, team] = interactionActor(body);
    const channel = text(record(body.channel).id);
    const threadTs = clickThread(body);
    if (!(await this.answers.admitted(user, team, channel, threadTs)) || channel === null) return;
    await this.answers.replyOnFailure(
      channel,
      threadTs,
      () => this.resumeClicked(channel, body),
      (told) => this.answers.inChannel(channel, told),
    );
  };

  private async resumeClicked(channel: string, body: Payload): Promise<void> {
    const { config, sessions, state } = this.parts;
    const record_ = state.channel(channel);
    if (record_ === null) {
      await this.answers.inChannel(channel, fill(texts.UNBOUND, { root: config.allowedRoot }));
      return;
    }
    // The button is not trusted: the session must still belong to the channel's directory,
    // and the thread it names must be well formed (`resumeIntoThread` refuses one that
    // already holds a session).
    const parsed = parseResumeValue(str(firstAction(body).value));
    if (parsed === null) {
      // A list posted before the value carried its thread (a bare session id).
      await this.answers.inChannel(channel, texts.RESUME_STALE);
      return;
    }
    const [sessionId, threadTs] = parsed;
    const stored = await sessions.sessionsIn(record_.directory);
    const chosen = stored.find((session) => session.id === sessionId);
    if (chosen === undefined) {
      await this.answers.inChannel(channel, texts.RESUME_GONE);
      return;
    }
    await this.resumeIntoThread(channel, threadTs, record_.directory, chosen, messageTs(body));
  }

  /**
   * The picker has done its job once a session is resumed from it. With `remove`, the
   * confirmation is in the thread and is the one record of what was resumed: the picker is
   * deleted (issue #70). Without it, or when the delete fails, the picker is rewritten into a
   * line that says what was resumed and where (an edit: silent), so its buttons never stay live.
   */
  async showResumed(
    channel: string,
    threadTs: string,
    listTs: string | null,
    chosen: ListedSession,
    options: { readonly remove: boolean },
  ): Promise<void> {
    const { slack, limiter } = this.parts;
    if (listTs === null || this.answers.stopped) return;
    if (options.remove) {
      try {
        await slack.chat.delete({ channel, ts: listTs });
        return;
      } catch (error) {
        if (isCancelled(error)) throw error;
        logger.warning(`could not delete the resume list in ${channel}: ${describe(error)}`);
      }
    }
    const title = mrkdwnEscape(oneLine(chosen.title, TITLE_LIMIT)) || chosen.id;
    const link = await this.answers.threadMrkdwnLink(channel, threadTs, "this thread");
    const shown = fill(texts.RESUME_LISTED, { title, link });
    try {
      await limiter.acquire();
      await slack.chat.update({
        channel,
        ts: listTs,
        text: shown,
        blocks: [contextBlock(noticeText(shown))],
      });
    } catch (error) {
      if (isCancelled(error)) throw error;
      // The session is resumed either way; a picker that keeps its buttons only refuses.
      logger.warning(`could not update the resume list in ${channel}: ${describe(error)}`);
    }
  }
}
