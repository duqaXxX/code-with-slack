/**
 * The questions the daemon puts to the owner, and the clicks that answer them: a tool's
 * permission request and Claude's own questions (`core/requests.ts`), the same-folder hold and
 * the session setup (`core/hold.ts`). Port of `ask_owner`, `hold_before_sending`,
 * `setup_before_sending` and the `on_hold_decision`, `on_setup_*`, `on_decision` and
 * `on_question_*` listeners of `slack_app.py`.
 */
import type { KnownBlock, ModalView } from "@slack/web-api";
import type { ModelInfo } from "../../../agent/seam.ts";
import type { HoldAnswer, Pending as HoldPending } from "../../../core/hold.ts";
import { APPROVE, DENY } from "../../../core/requests.ts";
import { SessionClosed, SessionGone } from "../../../core/sessions/errors.ts";
import type { ThreadSession } from "../../../core/sessions/session.ts";
import { type Choice, DEFAULT_CHOICE } from "../../../core/setup.ts";
import * as texts from "../../../core/texts.ts";
import { fill } from "../../../core/texts.ts";
import { HOLD_CONTINUE, holdBlocks } from "../hold.ts";
import { contextBlock, noticeText } from "../reply/blocks.ts";
import { describe } from "../reply/errors.ts";
import {
  APPROVAL_ALLOW,
  absorb,
  type Draft,
  draftAnswers,
  firstUnanswered,
  isAnswered,
  loadDraft,
  newDraft,
  questionView,
} from "../requests.ts";
import { readChoice, setupBlocks, summary as setupSummary } from "../setup.ts";
import { type Answers, type AppParts, errorName, isCancelled, logger } from "./answers.ts";
import { interactionActor, isOwner } from "./guards.ts";
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

/** What applies an answer while its hold is still open (`askOwner`). */
type Settle = (answer: HoldAnswer, shownAt: string, pending: HoldPending) => Promise<void>;

interface Question {
  /** The fallback text of the message. */
  readonly text: string;
  /** The blocks, given the id only its buttons carry. */
  readonly blocks: (holdId: string) => readonly object[];
  readonly settle?: Settle;
  /** What comes back with a click that carries only the id. */
  readonly context?: readonly ModelInfo[];
}

export class Requests {
  private readonly parts: AppParts;
  private readonly answers: Answers;

  constructor(parts: AppParts, answers: Answers) {
    this.parts = parts;
    this.answers = answers;
  }

  /**
   * The same-folder hold: post `Another session is working in this folder: <link>. Send
   * anyway?` and wait for the owner's Continue or Cancel, cancelled the same way by `!stop` (in
   * this thread or the whole channel) or a drain. True to send the message on; false when it
   * was not, either way telling the owner `Not sent.` already.
   */
  async holdBeforeSending(
    channel: string,
    threadTs: string,
    session: ThreadSession,
    other: ThreadSession,
  ): Promise<boolean> {
    const link = await this.answers.threadMrkdwnLink(other.channelId, other.threadTs, "Session");
    const answer = await this.askOwner(channel, threadTs, session, {
      text: fill(texts.HOLD_QUESTION, { link }),
      blocks: (holdId) => holdBlocks(holdId, link),
    });
    return answer === true;
  }

  /**
   * Ask for the session's model, effort and bypass before its first prompt, and apply the
   * answer to the live client. The session to send the message on, or null when it was not
   * sent, either way telling the owner `Not sent.` already. The client is connected first: the
   * CLI's own model list is what the message offers, and a folder that cannot start fails here,
   * before the owner picks anything.
   */
  async setupBeforeSending(
    channel: string,
    threadTs: string,
    asked: ThreadSession,
  ): Promise<ThreadSession | null> {
    const { sessions, state, slack, limiter } = this.parts;
    let session = asked;
    try {
      // First what an earlier, unsent Start left: the client it changed is dropped, so the
      // connect below reads the folder's own mode again.
      await session.forgetSetup();
      await session.ensureConnected();
    } catch (error) {
      if (!(error instanceof SessionClosed)) throw error;
      // Closed during the downloads before this: the retry of the submit never sees this
      // step, so the thread's fresh session is looked up here, as it does.
      const fresh = sessions.get(channel, threadTs);
      if (fresh === null) throw new SessionGone();
      session = fresh;
      await session.ensureConnected();
    }
    const live = session;
    const models = live.models;
    // Ticked when the connect above runs in bypass (a folder whose own settings start Claude
    // Code so): it is what would run, and unticking is the explicit off.
    const ticked = live.bypass;

    // Apply the choice, then turn the message into its summary line: all of it while the hold
    // is still open, so a stop that lands anywhere in here still cancels.
    const settle: Settle = async (answer, shownAt, pending) => {
      // A hold's Continue carries no choice: one clicked with a setup's id applies nothing.
      if (answer === true) throw new TypeError("a setup was answered with no choice");
      const choice: Choice = answer;
      await live.applySetup(choice);
      // The message stays as a record of what was set: no longer a request, not deleted.
      try {
        state.removeRequest(channel, threadTs, shownAt);
      } catch (error) {
        logger.warning(
          `could not clear a setup message from state.json in ${channel}/${threadTs}: ${errorName(error)}`,
        );
      }
      const line = setupSummary(models, choice);
      try {
        await limiter.acquire();
        if (pending.cancelled) return; // the cancel deleted the message during the wait: nothing to edit
        await slack.chat.update({
          channel,
          ts: shownAt,
          text: line,
          blocks: [contextBlock(noticeText(line))],
        });
      } catch (error) {
        if (isCancelled(error)) throw error;
        logger.warning(`could not update a setup message in ${channel}: ${describe(error)}`);
      }
    };

    const answer = await this.askOwner(channel, threadTs, live, {
      text: texts.SETUP_FALLBACK,
      blocks: (setupId) => setupBlocks(setupId, models, { ...DEFAULT_CHOICE, bypass: ticked }),
      settle,
      context: models,
    });
    if (answer === null) {
      await live.forgetSetup(); // a stop or drain that came while Start was applied
      return null;
    }
    return live;
  }

  /**
   * Post a question that holds the owner's message and wait for the answer: whatever its
   * buttons resolved it with, or null when it was cancelled (`!stop`, a drain) or never shown
   * (`Not sent.` or the unposted notice, told already). `blocks` gets the id only its buttons
   * carry; `context` comes back with a click that carries only that id. `settle(answer,
   * shownAt, pending)` applies an answer and writes its summary while the thread still shows
   * ✋ and the hold is still open, so a `!stop` or drain meanwhile cancels (`Pending.cancelled`:
   * nothing is sent, the message is removed, `Not sent.`). When it throws, the message is
   * removed, the hold ends as a cancel would and the error goes on.
   */
  async askOwner(
    channel: string,
    threadTs: string,
    session: ThreadSession,
    question: Question,
  ): Promise<HoldAnswer | null> {
    const { sessions, holds, slack, state } = this.parts;
    // (sync) The check and the hold it guards: a drain cancels every hold open when it starts
    // and never one opened after, so nothing is awaited between the two.
    if (sessions.draining) {
      // a restart could have started during an await before this
      await this.answers.refuseRestarting(channel, threadTs);
      return null;
    }
    const [holdId, pending] = holds.open(channel, threadTs, question.context ?? null);
    let posted: { ts?: unknown };
    try {
      posted = await slack.chat.postMessage({
        channel,
        thread_ts: threadTs,
        text: question.text,
        blocks: question.blocks(holdId) as unknown as KnownBlock[],
        unfurl_links: false,
        unfurl_media: false,
      });
    } catch (error) {
      if (isCancelled(error)) throw error;
      // Nobody can answer a question that was never shown: fail closed, as an unpostable
      // approval does, rather than send into a folder another session is using.
      logger.error(`could not post a hold in ${channel}/${threadTs}: ${describe(error)}`);
      holds.discard(holdId);
      await this.answers.tellOwner(channel, threadTs, texts.HOLD_UNPOSTED);
      return null;
    }
    const shownAt = str(posted.ts);
    // False when the answer came before `chat.postMessage` returned (a fast click, `!stop`, a
    // drain): nobody knew the ts then, so the message is still ours to settle or remove, and
    // `holdStart`/`holdEnd` (and their ✋) never run for it.
    const waiting = holds.posted(holdId, shownAt);
    if (waiting) {
      // Crash repair (issue #19): a hold question is a request like an approval. Best-effort:
      // outside the try/finally below on purpose, so a failed write here can never skip
      // `holdStart`/`holdEnd` and leave the hold itself undiscarded; the message is already
      // live either way, so a failure is logged loudly.
      try {
        state.addRequest(channel, threadTs, shownAt);
      } catch (error) {
        logger.error(
          `posted a hold in ${channel}/${threadTs} that state.json could not record: ${errorName(error)}`,
        );
      }
    }
    let answer: HoldAnswer | null = null;
    let sent = false;
    try {
      if (waiting) session.holdStart();
      answer = await pending.answer;
      if (answer === null) {
        if (!waiting) await this.answers.removeRequest(channel, threadTs, shownAt);
      } else if (question.settle !== undefined) {
        try {
          await question.settle(answer, shownAt, pending);
        } catch (error) {
          if (isCancelled(error)) throw error;
          if (!pending.cancelled) {
            await this.answers.removeRequest(channel, threadTs, shownAt);
            throw error;
          }
          logger.warning(
            `a cancelled hold in ${channel}/${threadTs} failed while applied: ${errorName(error)}`,
          );
        }
      }
      if (pending.cancelled) {
        answer = null;
        if (!waiting) await this.answers.removeRequest(channel, threadTs, shownAt);
      }
      sent = answer !== null;
    } finally {
      if (waiting) await session.holdEnd({ continued: sent });
      holds.discard(holdId); // catches a cancelled wait, and ends an answered hold
    }
    if (answer === null) await this.answers.tellOwner(channel, threadTs, texts.NOT_SENT);
    return answer;
  }

  // Issue #142: three paths answer `texts.HOLD_GONE`, and only these facts tell them apart.
  // Ids and flags only: the click's text and the setup's choice never reach the log.
  private logRefusedClick(path: string, body: Payload, facts: Record<string, boolean>): void {
    const told = Object.entries(facts)
      .map(([name, value]) => `${name}=${value ? "True" : "False"}`)
      .join(", ");
    logger.info(
      `refused a click (${path}): action ${str(firstAction(body).action_id)} in ` +
        `${str(record(body.channel).id)}/${clickThread(body)} on message ` +
        `${str(record(body.message).ts)}; ${told}`,
    );
  }

  /** Send anyway, or Don't send: the two buttons of the same-folder hold. */
  onHoldDecision = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    const { holds } = this.parts;
    const [user, team] = interactionActor(body);
    const channel = text(record(body.channel).id);
    const threadTs = clickThread(body);
    if (!(await this.answers.admitted(user, team, channel, threadTs)) || channel === null) return;
    const action = firstAction(body);
    const holdId = str(action.value);
    const answer = action.action_id === HOLD_CONTINUE ? true : null;
    // (sync) What the log says of a refused click is read with the resolve it explains.
    const held = holds.get(holdId);
    if (holds.resolve(holdId, channel, threadTs, answer) === null) {
      this.logRefusedClick("hold decision", body, {
        held: held !== null,
        decided: held?.decided ?? false,
      });
      await this.answers.tellOwner(channel, threadTs, texts.HOLD_GONE);
      return;
    }
    await this.answers.removeRequest(channel, threadTs, messageTs(body));
  };

  /** Start, in a session's setup. */
  onSetupStart = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    const { holds } = this.parts;
    const [user, team] = interactionActor(body);
    const channel = text(record(body.channel).id);
    const threadTs = clickThread(body);
    if (!(await this.answers.admitted(user, team, channel, threadTs)) || channel === null) return;
    const setupId = str(firstAction(body).value);
    const pending = holds.get(setupId);
    if (pending === null) {
      this.logRefusedClick("setup start, not held", body, { held: false });
      await this.answers.tellOwner(channel, threadTs, texts.HOLD_GONE);
      return;
    }
    // The controls' own state rides on the click: nothing about the choice is stored here,
    // and the model list is the one the message was built from.
    const choice = readChoice(record(body.state).values, pending.context ?? []);
    if (holds.resolve(setupId, channel, threadTs, choice) === null) {
      this.logRefusedClick("setup start, not resolved", body, {
        held: true,
        decided: pending.decided,
        same_thread: pending.channelId === channel && pending.threadTs === threadTs,
      });
      await this.answers.tellOwner(channel, threadTs, texts.HOLD_GONE);
      return;
    }
    logger.info(
      `accepted a setup start in ${channel}/${threadTs} on message ${str(record(body.message).ts)}`,
    );
  };

  /**
   * A new model changes which efforts exist: the message is rewritten with the levels of the
   * model now chosen, keeping the rest. An edit never rings a phone.
   */
  onSetupModel = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    const { holds, limiter, slack } = this.parts;
    const [user, team] = interactionActor(body);
    const channel = text(record(body.channel).id);
    const threadTs = clickThread(body);
    if (!(await this.answers.admitted(user, team, channel, threadTs)) || channel === null) return;
    const shownAt = messageTs(body);
    const setupId = holds.atMessage(channel, threadTs, shownAt);
    const pending = setupId === null ? null : holds.get(setupId);
    if (setupId === null || pending === null) {
      this.logRefusedClick("setup model, no open setup", body, {
        open_at_message: setupId !== null,
        held: pending !== null,
      });
      await this.answers.tellOwner(channel, threadTs, texts.HOLD_GONE);
      return;
    }
    const models = pending.context ?? [];
    const choice = readChoice(record(body.state).values, models);
    try {
      await limiter.acquire();
      // The wait above can outlast Start: a decided setup now shows its summary, which this
      // edit must not overwrite.
      if (holds.atMessage(channel, threadTs, shownAt) !== setupId) return;
      await slack.chat.update({
        channel,
        ts: shownAt,
        text: texts.SETUP_FALLBACK,
        blocks: setupBlocks(setupId, models, choice) as unknown as KnownBlock[],
      });
    } catch (error) {
      if (isCancelled(error)) throw error;
      // The controls keep what they showed; Start still reads the model and the effort.
      logger.warning(`could not update a setup message in ${channel}: ${describe(error)}`);
    }
  };

  /** An effort or bypass change needs nothing: Start reads every control's state. */
  onSetupEdit = async (ack: Ack): Promise<void> => {
    await ack();
  };

  /** Approve, Deny or Skip on a request of Claude Code's. */
  onDecision = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    const { approvals } = this.parts;
    const [user, team] = interactionActor(body);
    const channel = text(record(body.channel).id);
    const threadTs = clickThread(body);
    if (!(await this.answers.admitted(user, team, channel, threadTs)) || channel === null) return;
    const action = firstAction(body);
    const approvalId = str(action.value);
    const pending = approvals.get(approvalId);
    if (pending === null || pending.channelId !== channel || pending.threadTs !== threadTs) {
      await this.answers.tellOwner(channel, threadTs, texts.APPROVAL_GONE);
      return;
    }
    const outcome = action.action_id === APPROVAL_ALLOW ? APPROVE : DENY;
    if (approvals.resolve(approvalId, channel, threadTs, outcome) === null) {
      await this.answers.tellOwner(channel, threadTs, texts.APPROVAL_GONE);
      return;
    }
    await this.answers.removeRequest(channel, threadTs, messageTs(body));
  };

  /** A menu inside a question: its value is read when Submit is clicked. */
  onAnswer = async (ack: Ack): Promise<void> => {
    await ack();
  };

  /** Answer, on a question of Claude's: opens the form. */
  onQuestionOpen = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    const { approvals, slack } = this.parts;
    const [user, team] = interactionActor(body);
    const channel = text(record(body.channel).id);
    const threadTs = clickThread(body);
    if (!(await this.answers.admitted(user, team, channel, threadTs)) || channel === null) return;
    const approvalId = str(firstAction(body).value);
    const pending = approvals.get(approvalId);
    if (
      pending === null ||
      pending.channelId !== channel ||
      pending.threadTs !== threadTs ||
      pending.questions === null ||
      pending.questions.length === 0
    ) {
      await this.answers.tellOwner(channel, threadTs, texts.APPROVAL_GONE);
      return;
    }
    // trigger_id lives 3 seconds: the checks above are the only work before this call.
    const view = questionView(newDraft(approvalId, channel, threadTs), pending.questions);
    try {
      const trigger = text(body.trigger_id);
      if (trigger === null) throw new TypeError("the click carries no trigger_id");
      await slack.views.open({ trigger_id: trigger, view: view as unknown as ModalView });
    } catch (error) {
      if (isCancelled(error)) throw error;
      // an expired trigger_id, say: the turn must not wait unseen
      const message = fill(texts.QUESTION_NOT_OPENED, { error: describe(error) });
      await this.answers.tellOwner(channel, threadTs, message);
    }
  };

  /**
   * Next or Submit, in the question form. Slack wants the answer within 3 seconds, and a missing
   * answer can only be shown in it: the checks before it make no network call. The channel is
   * checked before acting.
   */
  onQuestionSubmit = async (ack: Ack, body: Payload): Promise<void> => {
    const { approvals, identity } = this.parts;
    const [user, team] = interactionActor(body);
    const view = record(body.view);
    let draft: Draft;
    try {
      draft = loadDraft(str(view.private_metadata));
    } catch {
      await ack();
      return;
    }
    if (!isOwner(identity, user, team)) {
      await ack();
      logger.info("ignored an inbound event from someone other than the owner");
      return;
    }
    const pending = approvals.get(draft.approvalId);
    if (
      pending === null ||
      pending.channelId !== draft.channelId ||
      pending.threadTs !== draft.threadTs ||
      pending.questions === null ||
      pending.questions.length === 0
    ) {
      await ack();
      await this.answers.tellOwner(draft.channelId, draft.threadTs, texts.APPROVAL_GONE);
      return;
    }
    const questions = pending.questions;
    draft = absorb(draft, record(view.state).values);
    if (!isAnswered(draft, questions, draft.active)) {
      await ack({
        response_action: "errors",
        errors: { [`q${draft.active}`]: texts.QUESTION_MISSING },
      });
      return;
    }
    const missing = firstUnanswered(draft, questions);
    if (missing !== null) {
      // Next: the following question, or one left unanswered (never, since each Next checks
      // its own; kept so a draft cannot reach Claude incomplete).
      const following = draft.active + 1 < questions.length ? draft.active + 1 : missing;
      await ack({
        response_action: "update",
        view: questionView({ ...draft, active: following }, questions),
      });
      return;
    }

    await ack();
    if (!(await this.answers.admitted(user, team, draft.channelId, draft.threadTs))) return;
    const answers = draftAnswers(draft, questions);
    if (answers === null) throw new Error("a draft with every question answered has no answers");
    const resolved = approvals.resolve(draft.approvalId, draft.channelId, draft.threadTs, {
      kind: "answer",
      answers,
    });
    if (resolved === null) {
      await this.answers.tellOwner(draft.channelId, draft.threadTs, texts.APPROVAL_GONE);
    }
    // The session that asked decides what becomes of the request message: its reply keeps the
    // answers, or the request itself does (`ThreadSession.keepAnswers`).
  };
}
