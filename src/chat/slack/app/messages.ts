/**
 * A message the owner wrote: a prompt for its thread's session, a word of the daemon's own, or
 * a command of Claude Code's. Port of `handle_message`, `handle_word`, `word_report`,
 * `old_folder_notice`, `submit_to_session`, `with_attachments`, `channel_status` and
 * `channel_status_row` of `slack_app.py`, with `slack_unescape`, `ago` and `is_clear`.
 *
 * Ordering. Python relied on `arrival_lock`, taken with no `await` after the checks, so that
 * prompts, messages with files and commands enter a thread's queue in the order they were sent
 * although downloads take a while. The same holds here by construction: from the moment the
 * checks of a message are over (`handleMessage` is called) to the moment its place in line is
 * taken (`Mutex.acquire` inside `arrival`), nothing is awaited unless Python awaited a call to
 * Slack there (the old-folder notice). The stretches are marked `(sync)`.
 */
import type { ListedSession, PromptContent } from "../../../agent/seam.ts";
import {
  type Command,
  helpText,
  hostOnly,
  type Passthrough,
  parseBang,
  refusedInThread,
  unformatted,
  WORD,
  type Word,
} from "../../../core/commands.ts";
import { oneLine } from "../../../core/reply/words.ts";
import { SessionClosed, SessionGone } from "../../../core/sessions/errors.ts";
import type { ThreadSession } from "../../../core/sessions/session.ts";
import { Mutex } from "../../../core/sessions/tasks.ts";
import * as texts from "../../../core/texts.ts";
import { fill } from "../../../core/texts.ts";
import {
  type DownloadedImage,
  DownloadFailed,
  imagesRefusal,
  isImage,
  limitFor,
  promptFor,
  refusal,
  save,
} from "../attachments.ts";
import { describe } from "../reply/errors.ts";
import { markdownEscape, mrkdwnEscape } from "../reply/escape.ts";
import { TITLE_LIMIT } from "../resume.ts";
import {
  type Answers,
  type AppParts,
  errorName,
  isCancelled,
  logger,
  type Report,
} from "./answers.ts";
import type { Open } from "./open.ts";
import type { Requests } from "./requests.ts";
import type { ResumeBind } from "./resume-bind.ts";
import { items, type Payload, str, text } from "./wire.ts";

// Slack sends a link as <url|label> or <url> (message formatting reference, read 2026-09-25).
// Mentions (<@U…>, <#C…>) stay as sent: naming them would need a scope the app does not have.
const LINK = /<((?:https?|mailto):[^|>]+)(?:\|([^>]+))?>/g;
const SCHEME = /^(?:https?:\/\/|mailto:)/;

function link(_whole: string, url: string, label: string | undefined): string {
  if (label === undefined) return url;
  // A link Slack made from a typed name carries that name as its label; one the owner named
  // keeps its address, which Claude needs to open it.
  return url.replace(SCHEME, "") === label ? label : `${label} (${url})`;
}

/** How long ago, in one unit, as a channel's `!status` says it of an idle session. */
export function ago(seconds: number): string {
  const whole = Math.trunc(seconds);
  if (whole < 60) return "just now";
  if (whole < 3600) return `${Math.floor(whole / 60)}m ago`;
  if (whole < 86400) return `${Math.floor(whole / 3600)}h ago`;
  return `${Math.floor(whole / 86400)}d ago`;
}

/** The text as the owner typed it, with the address of any link the owner named. */
export function slackUnescape(sent: string): string {
  return sent
    .replace(LINK, link)
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&amp;", "&");
}

/** The first word of a passthrough, as typed: Python's `text.split(" ", 1)[0]`. */
function firstWord(command: Passthrough): string {
  return command.text.split(" ", 1)[0] ?? "";
}

/**
 * Whether a passthrough is `!clear` under any of its names (`refusedInThread`, given the
 * session's `commands`): refused inside a thread (one thread is one session), left as an
 * ordinary passthrough everywhere else.
 */
export function isClear(command: Passthrough, commands: ThreadSession["commands"]): boolean {
  return refusedInThread(commands).has(firstWord(command).toLowerCase());
}

/** A Slack file object of a message: wire data, read field by field. */
type SlackFile = Readonly<Record<string, unknown>>;

/**
 * The files a message carries. A `files` that is not a list of objects throws, as reading it did
 * in Python: such a message never becomes a prompt, and never a word either.
 */
export function filesOf(event: Payload): SlackFile[] {
  const files = event.files;
  if (files === undefined || files === null) return [];
  if (!Array.isArray(files)) throw new TypeError("the message's files are not a list");
  return items(files).map((file) => {
    if (typeof file !== "object" || file === null || Array.isArray(file)) {
      throw new TypeError("a file of the message is not an object");
    }
    return file as SlackFile;
  });
}

/** What the message handlers call in the other parts. */
export interface Others {
  readonly requests: Requests;
  readonly open: Open;
  readonly resumeBind: ResumeBind;
}

export class Messages {
  private readonly parts: AppParts;
  private readonly answers: Answers;
  private readonly others: Others;
  // One lock per thread, alive only while someone holds or waits on it (a channel keeps
  // opening new threads for as long as it runs; a lock kept forever would leak).
  private readonly arrivalOrder = new Map<string, Mutex>();
  private readonly arrivalWaiters = new Map<string, number>();

  constructor(parts: AppParts, answers: Answers, others: Others) {
    this.parts = parts;
    this.answers = answers;
    this.others = others;
  }

  /** `work` in its turn among the messages of one thread: the turns are taken in call order. */
  private async arrival(
    channel: string,
    threadTs: string,
    work: () => Promise<void>,
  ): Promise<void> {
    const key = `${channel}\n${threadTs}`;
    // (sync) The place in line is taken inside this call, before its first await.
    this.arrivalWaiters.set(key, (this.arrivalWaiters.get(key) ?? 0) + 1);
    let lock = this.arrivalOrder.get(key);
    if (lock === undefined) {
      lock = new Mutex();
      this.arrivalOrder.set(key, lock);
    }
    const turn = lock.acquire();
    try {
      const release = await turn;
      try {
        await work();
      } finally {
        release();
      }
    } finally {
      const left = (this.arrivalWaiters.get(key) ?? 1) - 1;
      if (left === 0) {
        this.arrivalWaiters.delete(key);
        this.arrivalOrder.delete(key);
      } else {
        this.arrivalWaiters.set(key, left);
      }
    }
  }

  /**
   * One message that passed its checks, or a clip's transcript (`spoken`), routed by where it
   * was written: a known session's thread, a thread that holds none, or the channel.
   */
  async handleMessage(
    channel: string,
    threadTs: string,
    ts: string,
    event: Payload,
    options: { readonly spoken?: boolean } = {},
  ): Promise<void> {
    const { sessions, config } = this.parts;
    const spoken = options.spoken ?? false;
    // (sync) From here to the turn `submitToSession` takes, unless a call to Slack is due.
    const topLevel = threadTs === ts;
    const sent = slackUnescape(text(event.text) ?? "");
    const files = filesOf(event);
    // A message with files is a prompt: no daemon word or command takes a file. A word is a
    // word however it is formatted: the event's text carries the marks (a backtick before the
    // `!` of a message in inline code), which the composer's blocks tell from the text. Nor is
    // a clip's transcript (`spoken`): what Slack heard never turns bypass on, stops a session
    // or becomes a command, whatever its first character.
    let command: Command | null = null;
    if (files.length === 0 && !spoken) {
      command = parseBang(sent) ?? parseBang(unformatted(sent, event.blocks));
    }
    // A known session's thread routes a word to it; anywhere else (truly top-level, or a
    // thread that is not a session) a word acts exactly as a top-level one would.
    const session = topLevel ? null : sessions.get(channel, threadTs);
    if (command !== null && command.kind !== "passthrough") {
      const word = command;
      await this.answers.replyOnFailure(
        channel,
        threadTs,
        () =>
          this.handleWord(channel, threadTs, ts, word, {
            session,
            outside: !topLevel && session === null,
          }),
        this.wordReport(channel, threadTs, session),
      );
      return;
    }
    if (command !== null) {
      const answer = hostOnly(command);
      if (answer !== null) {
        // Answered where a word is: under the owner's message in a session's thread, for the
        // owner alone, and as a post in the channel anywhere else. No session starts.
        if (session !== null) await this.answers.tellOwner(channel, threadTs, answer);
        else await this.answers.inChannel(channel, answer);
        return;
      }
    }
    if (session !== null) {
      const moved = this.oldFolderText(channel, session);
      if (moved !== null) await this.oldFolderNotice(channel, threadTs, moved);
      await this.submitToSession(channel, threadTs, session, sent, files, command, {
        inThread: true,
      });
      return;
    }
    if (!topLevel) {
      // Anything but a daemon word, in a thread that holds no session: nowhere to send it.
      await this.answers.tellOwner(channel, threadTs, texts.NOT_A_SESSION);
      return;
    }
    const opened = sessions.open(channel, threadTs);
    if (opened === null) {
      await this.answers.inChannel(channel, fill(texts.UNBOUND, { root: config.allowedRoot }));
      return;
    }
    await this.submitToSession(channel, threadTs, opened, sent, files, command, {
      inThread: false,
    });
  }

  /**
   * Where a word's failure is told: in the channel for a word that acts top-level, under the
   * owner's message for one typed inside a session's thread.
   */
  wordReport(channel: string, threadTs: string, session: ThreadSession | null): Report {
    return async (told) => {
      if (session === null) await this.answers.inChannel(channel, told);
      else await this.answers.tellOwner(channel, threadTs, told);
    };
  }

  /** What the old-folder notice says of this thread, or null when its folder is the channel's. */
  private oldFolderText(channel: string, session: ThreadSession): string | null {
    const record = this.parts.state.channel(channel);
    if (record === null || session.directory === record.directory) return null;
    return fill(texts.OLD_THREAD_FOLDER, {
      old: mrkdwnEscape(session.directory),
      new: mrkdwnEscape(record.directory),
    });
  }

  /**
   * A prompt sent in a thread whose folder differs from the channel's current one is told so, on
   * every prompt: an ephemeral vanishes on reload, so a once-only notice could mean never. A
   * failed post must not drop the prompt that follows it: logged (ids only).
   */
  private async oldFolderNotice(channel: string, threadTs: string, moved: string): Promise<void> {
    try {
      await this.answers.notice(channel, threadTs, moved, { ephemeral: true });
    } catch (error) {
      if (isCancelled(error)) throw error;
      logger.warning(
        `could not post the old-folder notice in ${channel}/${threadTs}: ${describe(error)}`,
      );
    }
  }

  /**
   * Send a prompt, a message with files or a command to its thread's session, in the order the
   * thread's messages were sent, after the session's setup and the same-folder hold when either
   * is due.
   */
  async submitToSession(
    channel: string,
    threadTs: string,
    opened: ThreadSession,
    sent: string,
    files: readonly SlackFile[],
    command: Passthrough | null,
    options: { readonly inThread: boolean },
  ): Promise<void> {
    const { sessions } = this.parts;
    const { requests } = this.others;
    const { inThread } = options;
    if (this.answers.stopped) return;
    if (inThread && command !== null && isClear(command, opened.commands)) {
      await this.answers.tellOwner(channel, threadTs, texts.CLEAR_IN_THREAD);
      return;
    }
    // Prompts and commands for Claude Code enter the queue in the order they were sent,
    // although files take a while to download. Every reply goes to the thread.
    await this.arrival(channel, threadTs, async () => {
      let session = opened;
      let prompt: PromptContent | null =
        files.length > 0 ? await this.withAttachments(channel, threadTs, sent, files) : sent;
      if (prompt === null || this.answers.stopped) return;
      // Checked before the hold too: a drain already cancelled every hold open when it
      // started (`SessionManager.drain`) and will never cancel one opened after, so a message
      // that arrives once draining has begun must never open a new one (it would wait
      // forever).
      if (sessions.draining) {
        await this.answers.refuseRestarting(channel, threadTs);
        return;
      }
      // The same-folder hold: this message would wake `session` (busy just queues behind what
      // is already running); ask first when another live session, of any channel, is already
      // busy in the same resolved folder. Later messages of this thread queue behind the wait,
      // since it runs inside the arrival lock. `held` is the object a Continue's ✋ was left
      // standing on (`holdEnd({ continued: true })` bets on the `submit()` below to replace it
      // with ⏳): kept apart from `session`, which a `SessionClosed` retry below can reassign,
      // so a non-submit exit always restores the reaction on the object that actually shows it.
      let held: ThreadSession | null = null;
      let askedSetup = false;
      if (!inThread || session.neverRan) {
        // A session's first prompt: its setup comes before anything else, the hold's question
        // included. A reply in a thread whose setup was cancelled, or whose hold was, is a
        // first prompt too: nothing was ever sent in it.
        const ready = await requests.setupBeforeSending(channel, threadTs, session);
        if (ready === null) return;
        session = ready;
        held = ready;
        askedSetup = true;
      }
      if (!session.busy) {
        const other = sessions.workingIn({ besides: session });
        if (other !== null) {
          if (!(await requests.holdBeforeSending(channel, threadTs, session, other))) {
            // the Start that nothing was sent for
            if (askedSetup && !this.answers.stopped) await session.forgetSetup();
            return;
          }
          held = session;
        }
      }
      // Retried once against a freshly looked-up session: the one this call was handed can
      // still close under it (most likely the idle close, though `touch()` at the lookup
      // already guards the common case) during the download above or the steps below.
      let submitted = false;
      let failed = false;
      try {
        for (let attempt = 0; attempt < 2; attempt += 1) {
          try {
            if (command !== null) {
              await session.ensureConnected();
              const known = new Set(session.commands.map((offered) => offered.name));
              prompt = known.has(firstWord(command)) ? `/${command.text}` : sent;
            }
            // (sync) Checked last, with no await before the submit: a stop can start during a
            // download. The daemon's words still work meanwhile (`!stop` shortens the wait); a
            // new turn would not finish, and Slack does not resend this event. A stop that ended
            // meanwhile (the daemon is gone) sends nothing and says nothing.
            if (this.answers.stopped) return;
            if (sessions.draining) {
              await this.answers.refuseRestarting(channel, threadTs);
              return;
            }
            await session.submit(prompt);
            submitted = true;
            return;
          } catch (error) {
            if (!(error instanceof SessionClosed) || attempt > 0) throw error;
            const fresh = sessions.get(channel, threadTs);
            // Not just closed: its thread's own entry is gone too (a `SessionGone` close), so
            // a retry would find nothing here again either.
            if (fresh === null) throw new SessionGone();
            session = fresh;
          }
        }
      } catch (error) {
        failed = true;
        throw error;
      } finally {
        // `submitted` alone, not a fixed set of error types: `ensureConnected` can also reject
        // with the agent's own failure (a logged-out CLI, most likely) or anything a stray bug
        // throws; none of them may ever leave a Continue's ✋ standing forever. A failure gets
        // the reaction a turn that reached the queue and then failed gets; a plain return (the
        // drain notice posted fine) restores a Cancel's own reaction instead.
        if (held !== null && !submitted) await held.reactHoldAbandoned({ error: failed });
      }
    });
  }

  /**
   * The prompt for `sent` and its files; null, after telling the owner why, when any file cannot
   * reach Claude: the message is sent whole or not at all.
   */
  private async withAttachments(
    channel: string,
    threadTs: string,
    sent: string,
    files: readonly SlackFile[],
  ): Promise<PromptContent | null> {
    const { fetch, uploads } = this.parts;
    const refuse = async (file: SlackFile, reason: string): Promise<void> => {
      const name = mrkdwnEscape(str(file.name || file.id));
      await this.answers.tellOwner(channel, threadTs, fill(texts.UPLOAD_FAILED, { name, reason }));
    };

    for (const file of files) {
      const reason = refusal(file);
      if (reason !== null) {
        await refuse(file, reason);
        return null;
      }
    }
    const together = imagesRefusal(files);
    if (together !== null) {
      await this.answers.tellOwner(channel, threadTs, together);
      return null;
    }
    const fetched = await Promise.allSettled(
      files.map((file) =>
        fetch({
          url: str(file.url_private_download),
          mimetype: str(file.mimetype),
          limit: limitFor(file),
        }),
      ),
    );
    // Nothing is saved until every file arrived: a failed message leaves no copy behind.
    for (const [index, outcome] of fetched.entries()) {
      if (outcome.status === "fulfilled") continue;
      if (outcome.reason instanceof DownloadFailed) {
        await refuse(
          files[index] as SlackFile,
          fill(texts.UPLOAD_DOWNLOAD, { error: outcome.reason.message }),
        );
        return null;
      }
      throw outcome.reason;
    }
    const images: DownloadedImage[] = [];
    const paths: string[] = [];
    for (const [index, outcome] of fetched.entries()) {
      const file = files[index] as SlackFile;
      if (outcome.status !== "fulfilled") continue;
      if (isImage(file)) {
        images.push({ mediaType: str(file.mimetype), data: outcome.value });
        continue;
      }
      try {
        paths.push(await save(uploads, file, outcome.value));
      } catch (error) {
        if (!(error instanceof DownloadFailed)) throw error;
        await refuse(file, error.message);
        return null;
      }
    }
    return promptFor(sent, images, paths);
  }

  /**
   * A word of the daemon's own. Typed in the channel (or in a thread that holds no session) it
   * is answered by a normal post in the channel; typed inside a session's thread it is answered
   * for the owner alone under their message, or by a reaction, so nothing rings a phone; `!stop`
   * and `!open` are the exceptions: `!stop` is answered by a post that stays in the thread, and
   * `!open` posts its picker there and shares the file into it. `ts` is the word's own message.
   * `outside` is a thread that holds no session: a word typed there reads as being about that
   * thread, so the ones that act on the whole channel (`!stop`, `!resume`, `!bind` with a
   * folder) say where to send them, for the owner alone, and change nothing.
   */
  async handleWord(
    channel: string,
    threadTs: string,
    ts: string,
    command: Word,
    options: { readonly session: ThreadSession | null; readonly outside?: boolean },
  ): Promise<void> {
    const { sessions } = this.parts;
    const { open, resumeBind } = this.others;
    const { session } = options;
    const outside = options.outside ?? false;
    const where = session === null ? null : threadTs;
    const quiet = session !== null;
    switch (command.kind) {
      case "help":
      case "invalid": {
        // A mistyped word gets the full list, which shows how each word is written.
        const query = command.kind === "help" ? command.query : "";
        if (session !== null) await session.ensureConnected();
        const commands = session === null ? null : session.commands;
        await this.answers.say(channel, where, helpText(commands, query, markdownEscape), {
          ephemeral: quiet,
        });
        return;
      }
      case "guide":
        await this.answers.say(channel, where, texts.GUIDE, { ephemeral: quiet });
        return;
      case "bind":
        if (session !== null || (outside && command.path)) {
          await this.answers.tellOwner(
            channel,
            threadTs,
            fill(texts.WORD_IN_THREAD, { word: WORD.bind }),
          );
        } else if (command.path) {
          await resumeBind.bindTo(channel, command.path);
        } else {
          await resumeBind.listFolders(channel);
        }
        return;
      case "bypass":
        if (session === null) {
          await this.answers.inChannel(channel, texts.BYPASS_TOP_LEVEL);
        } else if (!(await session.switchBypass(command.on))) {
          // Start writes the setup's box over the switch (`applySetup`): one flipped before
          // it would not hold, so the word changed nothing.
          await this.answers.tellOwner(channel, threadTs, texts.BYPASS_BEFORE_START);
        } else {
          // Both: the line says what changed and is gone on reload, the ✅ stays.
          await this.answers.acknowledge(channel, ts);
          await this.answers.tellOwner(
            channel,
            threadTs,
            command.on ? texts.BYPASS_ON_THREAD : texts.BYPASS_OFF_THREAD,
          );
        }
        return;
      case "status":
        if (session === null) await this.channelStatus(channel);
        else await this.answers.say(channel, threadTs, await session.status(), { ephemeral: true });
        return;
      case "stop":
        if (outside) {
          await this.answers.tellOwner(channel, threadTs, texts.STOP_OUTSIDE_SESSION);
        } else if (session === null) {
          const stopped = await sessions.stopChannel(channel);
          // null: only a hold was cancelled somewhere in the channel (`Not sent.`, from its
          // own waiter); no second notice, since nothing Claude Code was doing stopped.
          if (stopped !== null) {
            await this.answers.inChannel(
              channel,
              stopped ? texts.STOPPED_CHANNEL : texts.NOTHING_TO_STOP,
            );
          }
        } else {
          // Either answer is a post that stays in the thread: an ephemeral line is gone on
          // reload, and the root's ✅ alone does not say a stop was received. null: only a
          // hold was cancelled, which already said `Not sent.` from its own waiter.
          const stopped = await session.stop();
          if (stopped) {
            // The answer goes under the ending and the footer of the reply the stop cut
            // short, which may be a message of its own.
            await session.stopLanded();
          }
          if (stopped !== null) {
            // Through `tellOwner`: the stop itself worked, so a post that fails must not be
            // reported as the word's failure.
            await this.answers.tellOwner(
              channel,
              threadTs,
              stopped ? texts.STOPPED_THREAD : texts.NOTHING_TO_STOP_THREAD,
              { stays: true },
            );
          }
        }
        return;
      case "open":
        if (session === null) await this.answers.inChannel(channel, texts.OPEN_TOP_LEVEL);
        else await open.openWord(channel, threadTs, session, command.target);
        return;
      case "resume":
        if (session !== null || outside) {
          await this.answers.tellOwner(
            channel,
            threadTs,
            fill(texts.WORD_IN_THREAD, { word: WORD.resume }),
          );
        } else {
          await resumeBind.handleResume(channel, threadTs, command.target);
        }
        return;
    }
  }

  /** `!status` typed in the channel: its folder, then one line per live session. */
  async channelStatus(channel: string): Promise<void> {
    const { state, sessions, config } = this.parts;
    const record = state.channel(channel);
    if (record === null) {
      await this.answers.inChannel(channel, fill(texts.UNBOUND, { root: config.allowedRoot }));
      return;
    }
    const lines = [fill(texts.STATUS_CHANNEL_HEADER, { directory: record.directory })];
    const live = sessions.sessionsOf(channel);
    if (live.length === 0) {
      lines.push(texts.STATUS_CHANNEL_EMPTY);
    } else {
      // What tells one thread from another (issue #76): the title Claude Code gave its
      // session, as the Home tab and a restart's notice name it, and the time of its last
      // message. Not the time of its file, which the listing gives: Claude Code writes entries
      // with no timestamp to a transcript when it connects the session again, so a session
      // idle for 16 minutes read `just now` (seen 2026-10-07). Only the sessions the answer
      // shows are dated, since dating reads their files.
      const shown = new Set<string>();
      for (const session of live) {
        const stored = state.thread(channel, session.threadTs);
        if (stored?.sessionId) shown.add(stored.sessionId);
      }
      const known = new Map<string, ListedSession>();
      for (const directory of new Set(live.map((session) => session.directory))) {
        try {
          const listed = await sessions.sessionsIn(directory);
          const dated = await sessions.dated(
            directory,
            listed.filter((session) => shown.has(session.id)),
          );
          for (const session of dated) known.set(session.id, session);
        } catch (error) {
          if (isCancelled(error)) throw error;
          logger.warning(`could not list a folder's sessions: ${errorName(error)}`);
        }
      }
      lines.push(
        ...(await Promise.all(
          live.map((session) => this.channelStatusRow(channel, record.directory, session, known)),
        )),
      );
    }
    await this.answers.say(channel, null, lines.join("\n"));
    const waits = await this.answers.restartWaits(channel);
    if (waits) {
      // A notice of its own: the rows are mrkdwn, and the status above is a markdown block.
      await this.answers.inChannel(channel, [texts.RESTART_WAITS_HEADER, waits].join("\n"));
    }
  }

  private async channelStatusRow(
    channel: string,
    channelDirectory: string,
    session: ThreadSession,
    known: ReadonlyMap<string, ListedSession>,
  ): Promise<string> {
    const stored = this.parts.state.thread(channel, session.threadTs);
    const info = stored === null ? undefined : known.get(String(stored.sessionId));
    const title = info === undefined ? "" : oneLine(info.title, TITLE_LIMIT);
    const linked = await this.answers.threadLink(channel, session.threadTs, title);
    let activity: string;
    if (session.waitingForOwner) {
      activity = texts.STATUS_CHANNEL_WAITING;
    } else if (session.busy || session.runningKinds) {
      // A session with only a background task running is not idle either.
      activity = texts.STATUS_CHANNEL_BUSY;
    } else {
      activity = texts.STATUS_CHANNEL_IDLE;
      if (info?.lastModified) {
        // In milliseconds, and by now the time of the session's last message
        // (`channelStatus` dated it).
        const since = ago(this.parts.now() - info.lastModified / 1000);
        activity += fill(texts.STATUS_CHANNEL_SINCE, { ago: since });
      }
    }
    let row = fill(texts.STATUS_CHANNEL_ROW, { link: linked, activity });
    if (session.runningKinds) {
      row += ` · ${fill(texts.RUNNING, { counts: session.runningKinds })}`;
    }
    if (session.bypass) row += texts.STATUS_CHANNEL_BYPASS;
    if (session.directory !== channelDirectory) {
      row += fill(texts.STATUS_CHANNEL_FOLDER, { directory: session.directory });
    }
    return row;
  }
}
