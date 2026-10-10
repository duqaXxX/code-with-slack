/** What the daemon asks Slack about the channels it is bound to. */
import type { WebClient } from "@slack/web-api";
import type { ChannelLookup } from "../../core/cleanup.ts";
import { ChatError } from "../seam.ts";
import { describe } from "./reply/errors.ts";

const CHANNEL_NOT_FOUND = "channel_not_found";

/**
 * What the cleanup asks about a bound channel: `conversations.info`. Only `channel_not_found` is
 * an answer that the channel is gone; any other failure crosses as a `ChatError` named by
 * Slack's code, which the cleanup logs as no answer.
 */
export function channelLookup(slack: WebClient): ChannelLookup {
  return async (channelId) => {
    try {
      await slack.conversations.info({ channel: channelId });
      return "there";
    } catch (error) {
      const code = describe(error);
      if (code === CHANNEL_NOT_FOUND) return "gone";
      throw new ChatError(code, { cause: error });
    }
  };
}
