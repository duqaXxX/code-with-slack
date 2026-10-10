/** The footer as one line of Slack mrkdwn. */
import { basename } from "node:path";
import { type FooterKey, footerFields } from "../../core/footer.ts";
import type { FooterFields } from "../seam.ts";
import { mrkdwnEscape } from "./reply/escape.ts";

// The footer's fields that come before the folder.
const SESSION: readonly FooterKey[] = ["model", "effort"];

/** A field as the footer writes it: the label in bold (`*ctx* 15%`, the owner's choice). */
function short(key: FooterKey, value: string): string {
  switch (key) {
    case "model":
    case "changes":
      return value;
    case "effort":
      return `*effort* ${value}`;
    case "branch":
      return mrkdwnEscape(value);
    case "tokens":
      return `${value} *tok*`;
    case "context":
      return `*ctx* ${value}`;
    case "session_limit":
      return `*5h* ${value}`;
    case "week_limit":
      return `*7d* ${value}`;
  }
}

/**
 * One line: bypass, model and effort, the channel's folder, then branch, changes, tokens,
 * context and limits (the owner's order).
 */
export function formatFooter(data: FooterFields, now: number): string {
  const fields = footerFields(data, now);
  const parts = data.bypass ? ["⚡ bypass"] : [];
  parts.push(...fields.filter((f) => SESSION.includes(f.key)).map((f) => short(f.key, f.value)));
  // Its name alone, the project's: the whole path is on `!status`'s Directory line.
  const name = data.folder === null ? "" : basename(data.folder);
  if (name) {
    parts.push(mrkdwnEscape(name));
  }
  parts.push(...fields.filter((f) => !SESSION.includes(f.key)).map((f) => short(f.key, f.value)));
  return parts.join(" · ");
}
