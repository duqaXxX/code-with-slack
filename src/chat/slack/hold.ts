/** The same-folder hold's question as Slack blocks. */
import * as texts from "../../core/texts.ts";

export const HOLD_CONTINUE = "hold_continue";
export const HOLD_CANCEL = "hold_cancel";

/**
 * The question in the thread: a `mrkdwn` section (`link` in mrkdwn's own `<url|label>` form, same
 * as approvals and the resume picker use for their own buttons) plus Continue and Cancel. Only
 * Continue is `primary`: one button of a set, and never `danger` for an answer that destroys
 * nothing (button element reference, docs.slack.dev, read 2026-10-07).
 */
export function holdBlocks(holdId: string, link: string): Record<string, unknown>[] {
  return [
    {
      type: "section",
      text: { type: "mrkdwn", text: texts.fill(texts.HOLD_QUESTION, { link }) },
    },
    {
      type: "actions",
      elements: [
        {
          type: "button",
          action_id: HOLD_CONTINUE,
          value: holdId,
          style: "primary",
          text: { type: "plain_text", text: texts.HOLD_CONTINUE_BUTTON },
        },
        {
          type: "button",
          action_id: HOLD_CANCEL,
          value: holdId,
          text: { type: "plain_text", text: texts.HOLD_CANCEL_BUTTON },
        },
      ],
    },
  ];
}
