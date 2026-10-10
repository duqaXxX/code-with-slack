/**
 * The usage probe on a fake `query()`. The recorded `/usage` answer is `usage.jsonl`; the Python
 * test (`test_footer.py`) is the one with no answer, which monkeypatched `USAGE_TIMEOUT` to 50 ms
 * and here crosses the timeout on a `FakeClock`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { ClaudeBackend } from "../../../src/agent/claude/backend.ts";
import { USAGE_TIMEOUT } from "../../../src/agent/claude/usage.ts";
import { UsageProbe } from "../../../src/agent/claude/usage-probe.ts";
import { END, FakeSdk } from "../../support/fake-query.ts";
import { FakeClock } from "../../support/fake-slack.ts";
import { sdkRecords } from "../../support/fixtures.ts";

const LIMIT = { timeout: 10_000 };
const FOLDER = "/srv/alice/app";
// 2026-09-30 at 22:00 UTC: the recorded limits reset on the 1st and the 4th of October.
const NOW = Date.UTC(2026, 8, 30, 22, 0);

function probeOn(sdk: FakeSdk, clock = new FakeClock()): { probe: UsageProbe; clock: FakeClock } {
  clock.now = NOW / 1000;
  const backend = new ClaudeBackend({ query: sdk.query });
  const probe = new UsageProbe(FOLDER, backend.start.bind(backend), { clock });
  return { probe, clock };
}

/** Lets real file reads finish: the back end reads the owner's Chrome choice before it starts. */
async function started(sdk: FakeSdk): Promise<void> {
  while (sdk.queries.length === 0) await new Promise((resolve) => setTimeout(resolve, 1));
}

test("a usage probe with no answer gives up and closes", LIMIT, async () => {
  const sdk = new FakeSdk(); // no scripted turn: /usage never answers
  const { probe, clock } = probeOn(sdk);
  const reading = probe.read();
  const outcome = assert.rejects(reading, { name: "TimeoutError" });
  // The session exists before the time runs out, as in a real minute: on a loaded machine the
  // fake clock could otherwise cross the timeout while the start still read a file.
  await started(sdk);
  await clock.advance(USAGE_TIMEOUT / 1000);
  await outcome;
  assert.equal(sdk.only.closed, true); // the next refresh starts from a clean session
});

test(
  "a usage probe that times out before its session started closes it when it comes",
  LIMIT,
  async () => {
    const sdk = new FakeSdk();
    const { probe, clock } = probeOn(sdk);
    const outcome = assert.rejects(probe.read(), { name: "TimeoutError" });
    await clock.advance(USAGE_TIMEOUT / 1000); // at once: the start may still be on its way
    await outcome;
    await started(sdk);
    while (!sdk.only.closed) await new Promise((resolve) => setTimeout(resolve, 1));
    assert.equal(sdk.only.closed, true);
  },
);

test("the probe asks /usage and reads the limits off the answer", LIMIT, async () => {
  const sdk = new FakeSdk({ turns: [sdkRecords("usage")] });
  const { probe } = probeOn(sdk);
  const usage = await probe.read();
  assert.equal(usage.session?.percent, 5);
  assert.equal(usage.week?.percent, 29);
  assert.equal(sdk.only.sent[0]?.message.content, "/usage");
  await probe.close();
});

test("the probe takes no setting source of the owner's", LIMIT, async () => {
  const sdk = new FakeSdk({ turns: [sdkRecords("usage")] });
  const { probe } = probeOn(sdk);
  await probe.read();
  assert.deepEqual(sdk.only.options.settingSources, []);
  assert.equal(sdk.only.options.cwd, FOLDER);
  assert.equal("resume" in sdk.only.options, false);
  await probe.close();
});

test("the probe keeps its session for the next read", LIMIT, async () => {
  const sdk = new FakeSdk({ turns: [sdkRecords("usage"), sdkRecords("usage")] });
  const { probe } = probeOn(sdk);
  await probe.read();
  await probe.read();
  assert.equal(sdk.queries.length, 1);
  assert.equal(sdk.only.sent.length, 2);
  await probe.close();
});

test("after a failure the next read starts a clean session", LIMIT, async () => {
  const sdk = new FakeSdk((index) => ({ turns: [index === 0 ? [END] : sdkRecords("usage")] }));
  const { probe } = probeOn(sdk);
  await assert.rejects(probe.read());
  assert.equal(sdk.queries[0]?.closed, true);
  const usage = await probe.read();
  assert.equal(sdk.queries.length, 2);
  assert.equal(usage.session?.percent, 5);
  await probe.close();
});

test("an answer that holds no limits reads as none", LIMIT, async () => {
  const bare = sdkRecords("usage").map((record) =>
    record.type === "result" ? { ...record, result: "Nothing to show" } : record,
  );
  const sdk = new FakeSdk({ turns: [bare] });
  const { probe } = probeOn(sdk);
  assert.deepEqual(await probe.read(), { session: null, week: null });
  await probe.close();
});

test("close is idempotent, and a read after it starts again", LIMIT, async () => {
  const sdk = new FakeSdk({ turns: [sdkRecords("usage")] });
  const { probe } = probeOn(sdk);
  await probe.close();
  await probe.close();
  assert.equal(sdk.queries.length, 0);
  await probe.read();
  await probe.close();
  await probe.close();
  assert.equal(sdk.only.calls.filter((call) => call.method === "close").length, 1);
});
