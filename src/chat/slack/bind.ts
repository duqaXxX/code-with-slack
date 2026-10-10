/**
 * `!bind` alone, as Slack shows it: one row per folder `src/core/folders.ts` found, a Bind button
 * each.
 */
import { relative, sep } from "node:path";
import { FOLDER_ROWS } from "../../core/folders.ts";
import * as texts from "../../core/texts.ts";
import { fill } from "../../core/texts.ts";
import { type ContextBlock, contextBlock } from "./reply/blocks.ts";
import { shownAsWritten } from "./reply/escape.ts";

export const BIND_ACTION = "folder_bind";

/** One folder: its relative path as code, and a Bind button unless it is the channel's own. */
export interface FolderRow {
  type: "section";
  block_id: string;
  text: { type: "mrkdwn"; text: string };
  accessory?: {
    type: "button";
    action_id: string;
    value: string;
    text: { type: "plain_text"; text: string };
  };
}

export type BindBlock = ContextBlock | FolderRow;

/** A path's components as `PurePosixPath` compares them: no empty or `.` part. */
function parts(path: string): string[] {
  return path.split("/").filter((part) => part !== "" && part !== ".");
}

function samePath(a: string, b: string): boolean {
  const left = parts(a);
  const right = parts(b);
  return left.length === right.length && left.every((part, i) => part === right[i]);
}

/** Python's order on code points, where `<` on JavaScript strings goes by UTF-16 unit. */
function compareParts(a: string, b: string): number {
  const left = Array.from(a);
  const right = Array.from(b);
  for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
    const x = left[i] as string;
    const y = right[i] as string;
    if (x !== y) return (x.codePointAt(0) as number) - (y.codePointAt(0) as number);
  }
  return left.length - right.length;
}

/** Python sorts a `Path` by its components, so `a/b` comes before `a-b`; a string sort would not. */
function comparePaths(a: string, b: string): number {
  const left = parts(a);
  const right = parts(b);
  for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
    const order = compareParts(left[i] as string, right[i] as string);
    if (order !== 0) return order;
  }
  return left.length - right.length;
}

function row(root: string, index: number, folder: string, current: boolean): FolderRow {
  const shown = relative(root, folder).split(sep).join("/") || ".";
  const block: FolderRow = {
    type: "section",
    // An index, not the path: a block_id is capped at 255 characters.
    block_id: `folder-${index}`,
    text: {
      type: "mrkdwn",
      text: `\`${shownAsWritten(shown)}\`${current ? texts.BIND_CURRENT : ""}`,
    },
  };
  if (!current) {
    block.accessory = {
      type: "button",
      action_id: BIND_ACTION,
      value: shown,
      text: { type: "plain_text", text: texts.BIND_BUTTON },
    };
  }
  return block;
}

/** The list: the first FOLDER_ROWS folders in path order, the channel's own marked. */
export function bindBlocks(root: string, folders: string[], current: string | null): BindBlock[] {
  const shown = folders.slice(0, FOLDER_ROWS).toSorted(comparePaths);
  // The list's own lines are the daemon's notices, small and grey; the rows keep their button.
  if (folders.length === 0) {
    return [contextBlock(fill(texts.BIND_EMPTY, { root: shownAsWritten(root) }))];
  }
  return [
    contextBlock(fill(texts.BIND_LIST, { root: shownAsWritten(root) })),
    ...shown.map((folder, i) =>
      row(root, i, folder, current !== null && samePath(folder, current)),
    ),
    ...(folders.length > FOLDER_ROWS
      ? [contextBlock(fill(texts.BIND_MORE, { rows: FOLDER_ROWS }))]
      : []),
  ];
}
