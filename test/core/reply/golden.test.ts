/**
 * Parity at the reply seam: for every recorded SDK stream, the calls this renderer makes on its
 * sink are the calls Python's `TurnRenderer` made, one for one.
 *
 * `test/golden/reply/<name>.json` holds what Python did (`tests/golden.py`, seam 1): `whole` is
 * one renderer fed the whole recording, then closed as a finished turn is (`close("footer")`,
 * `close_out()`); `turns` is one fresh renderer per turn. Each holds the sink calls in order and
 * the renderer's end state. Here the recording goes through one `Translator`, as one session's
 * would, and its events through the renderer: a fresh renderer per turn in `turns`, the same
 * translator throughout.
 *
 * The footer is the golden's own string: the renderer never reads a footer, so it is generic in
 * what it hands to `closeOut`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import type { SessionEvent } from "../../../src/agent/seam.ts";
import type { TaskUpdate } from "../../../src/chat/seam.ts";
import { type Sink, TurnRenderer } from "../../../src/core/reply/renderer.ts";
import { golden, sdkRecordings } from "../../support/fixtures.ts";
import { recordedEvents, splitTurns } from "../../support/replay.ts";

const FOOTER = "footer";

type SinkCall =
  | { call: "text"; markdown: string; notice: boolean; ending: boolean }
  | { call: "task"; update: TaskUpdate }
  | { call: "finish"; closing: readonly TaskUpdate[] }
  | { call: "close_out"; footer: string | null }
  | { call: "wait_landed" }
  | { call: "settle" };

/** Every call the renderer makes on its sink, as the plain objects the golden files hold. */
class RecordingSink implements Sink<string> {
  calls: SinkCall[] = [];

  async text(
    markdown: string,
    options: { readonly notice?: boolean; readonly ending?: boolean } = {},
  ): Promise<void> {
    this.calls.push({
      call: "text",
      markdown,
      notice: options.notice ?? false,
      ending: options.ending ?? false,
    });
  }

  async task(update: TaskUpdate): Promise<void> {
    this.calls.push({ call: "task", update });
  }

  async finish(closing: readonly TaskUpdate[]): Promise<void> {
    this.calls.push({ call: "finish", closing });
  }

  async closeOut(footer: string | null): Promise<boolean> {
    this.calls.push({ call: "close_out", footer });
    return true;
  }

  async waitLanded(): Promise<boolean> {
    this.calls.push({ call: "wait_landed" });
    return true;
  }

  async settle(): Promise<boolean> {
    this.calls.push({ call: "settle" });
    return true;
  }
}

/** One renderer fed `events`, then closed as a finished turn is. */
async function replay(events: readonly SessionEvent[]): Promise<unknown> {
  const sink = new RecordingSink();
  const renderer = new TurnRenderer<string>(sink);
  for (const event of events) await renderer.feed(event);
  await renderer.close(FOOTER);
  await renderer.closeOut();
  return {
    calls: sink.calls,
    result_set: renderer.result !== null,
    running_tasks: renderer.runningTasks,
  };
}

for (const name of sdkRecordings()) {
  test(`the sink calls of ${name} are the ones python made [whole]`, async () => {
    assert.deepEqual(await replay(recordedEvents(name)), golden("reply", name).whole);
  });

  test(`the sink calls of ${name} are the ones python made [turns]`, async () => {
    const turns: unknown[] = [];
    for (const turn of splitTurns(recordedEvents(name))) turns.push(await replay(turn));
    assert.deepEqual(turns, golden("reply", name).turns);
  });
}
