import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { utimes } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { type TestContext, test } from "node:test";
import {
  DownloadFailed,
  download,
  type FetchFn,
  FILE_LIMIT,
  IMAGE_LIMIT,
  IMAGES_LIMIT,
  IMAGES_PER_MESSAGE,
  imagesRefusal,
  KEEP_SECONDS,
  logger,
  prepareUploads,
  promptFor,
  refusal,
  save,
  savedName,
} from "../../../src/chat/slack/attachments.ts";
import * as texts from "../../../src/core/texts.ts";
import { slackPayload } from "../../support/fixtures.ts";

const WINDOWS = { skip: process.platform === "win32" };
const TOKEN = "xox" + "b-fake";
const URL_OF_FILE = "https://files.slack.com/files-pri/T000TEAM-F000FILE/download/photo.png";

/** The file object of a recorded file_share message (scrubbed, Slack 2026-09-25). */
function shared(kind: "image" | "screenshot" | "snippet", fields: Record<string, unknown> = {}) {
  const name = { image: "100", screenshot: "101", snippet: "102" }[kind];
  const body = slackPayload(`${name}-event_callback-file_share-${kind}`);
  const event = body.event as { files: Record<string, unknown>[] };
  return { ...structuredClone(event.files[0]), ...fields };
}

function folderIn(t: TestContext): string {
  const root = mkdtempSync(join(tmpdir(), "awaydesk-test-"));
  t.after(() => {
    // A test may leave a folder unwritable or unreadable.
    for (const path of [join(root, "uploads"), root]) {
      try {
        chmodSync(path, 0o700);
      } catch {}
    }
    rmSync(root, { recursive: true, force: true });
  });
  return root;
}

test("recorded images and snippets are accepted", () => {
  assert.deepEqual(
    (["image", "screenshot", "snippet"] as const).map((kind) => refusal(shared(kind))),
    [null, null, null],
  );
});

// An image past Claude's limits is refused with the reason.
const IMAGE_REFUSALS: [
  string,
  Record<string, unknown>,
  (file: Record<string, unknown>) => string,
][] = [
  [
    "heic",
    { mimetype: "image/heic" },
    () => texts.fill(texts.UPLOAD_IMAGE_TYPE, { mimetype: "image/heic" }),
  ],
  [
    "size",
    { size: IMAGE_LIMIT + 1 },
    () => texts.fill(texts.UPLOAD_IMAGE_SIZE, { size: "7.2MB", limit: "7.2MB" }),
  ],
  [
    "side",
    { original_w: 8001 },
    (file) => texts.fill(texts.UPLOAD_IMAGE_SIDE, { width: 8001, height: String(file.original_h) }),
  ],
  ["check_file_info", { file_access: "check_file_info" }, () => texts.UPLOAD_NOT_SHARED],
  [
    "other host",
    { url_private_download: "https://evil.example/x.png" },
    () => texts.UPLOAD_NOT_SHARED,
  ],
  [
    "plain http",
    { url_private_download: "http://files.slack.com/x.png" },
    () => texts.UPLOAD_NOT_SHARED,
  ],
];
for (const [name, fields, reason] of IMAGE_REFUSALS) {
  test(`an image past claude s limits is refused with the reason [${name}]`, () => {
    const file = shared("image", fields);
    assert.equal(refusal(file), reason(file));
  });
}

test("a file past the limit is refused", () => {
  assert.equal(
    refusal(shared("snippet", { size: FILE_LIMIT + 1 })),
    texts.fill(texts.UPLOAD_FILE_SIZE, { size: "100.0MB", limit: "100.0MB" }),
  );
});

test("a saved name cannot leave the uploads folder", () => {
  assert.equal(savedName({ id: "F000FILE", name: "../../etc/passwd" }), "F000FILE-passwd");
  assert.equal(savedName({ id: "F000FILE", name: "" }), "F000FILE");
});

test("text and files make a plain prompt", () => {
  const prompt = promptFor("Summarize this file.", [], ["/tmp/cws/F1-notes.txt"]);
  assert.equal(prompt, "Summarize this file.\n\nAttached files:\n- /tmp/cws/F1-notes.txt");
});

// Python asserted on the SDK's block (`source: {type, media_type, data}`); the seam's image part
// is `{type, mediaType, data}`, and the Claude back end converts.
test("images make one message of content blocks", () => {
  const blocks = promptFor(
    "What is in this image?",
    [{ mediaType: "image/png", data: Buffer.from("\x89PNG", "latin1") }],
    [],
  );
  assert.deepEqual(blocks, [
    { type: "text", text: "What is in this image?" },
    {
      type: "image",
      mediaType: "image/png",
      data: Buffer.from("\x89PNG", "latin1").toString("base64"),
    },
  ]);
  const imageOnly = promptFor("", [{ mediaType: "image/png", data: Buffer.from("x") }], []);
  assert.ok(Array.isArray(imageOnly));
  assert.equal(imageOnly[0]?.type, "image");
});

/** A `fetch` that answers as the Python tests' server did, and records what it was asked. */
function answering(make: () => Response) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetch: FetchFn = async (url, init) => {
    calls.push({ url, init });
    return make();
  };
  return { fetch, calls };
}

test("download sends the bot token and returns the bytes", async () => {
  const server = answering(
    () => new Response("hello\n", { headers: { "content-type": "text/plain" } }),
  );
  const got = await download(URL_OF_FILE, TOKEN, "text/plain", 100, { fetch: server.fetch });
  assert.deepEqual(Buffer.from(got), Buffer.from("hello\n"));
  assert.equal(server.calls.length, 1);
  const headers = server.calls[0]?.init.headers as Record<string, string>;
  assert.deepEqual(headers, { Authorization: `Bearer ${TOKEN}` });
  // A redirect could carry the token to another host: none is followed.
  assert.equal(server.calls[0]?.init.redirect, "manual");
});

const FAILED_DOWNLOADS: [string, () => Response, RegExp][] = [
  ["HTTP 404", () => new Response(null, { status: 404 }), /HTTP 404/],
  // Measured 2026-09-25: without files:read Slack answers 302; with it, 200 and the file.
  [
    "302",
    () => new Response(null, { status: 302, headers: { Location: "https://elsewhere.example/" } }),
    /files:read/,
  ],
  [
    "larger than",
    () => new Response(new Uint8Array(101), { headers: { "content-type": "text/plain" } }),
    /larger than/,
  ],
  // Without files:read, Slack answers with its sign-in page instead of the file.
  [
    "web page",
    () => new Response("<html>", { headers: { "content-type": "text/html; charset=utf-8" } }),
    /files:read/,
  ],
];
for (const [name, response, error] of FAILED_DOWNLOADS) {
  test(`a failed download says why [${name}]`, async () => {
    const server = answering(response);
    await assert.rejects(download(URL_OF_FILE, TOKEN, "image/png", 100, { fetch: server.fetch }), {
      name: "DownloadFailed",
      message: error,
    });
  });
}

test("the token never leaves for another host", async () => {
  // Checked inside download too, beside the header it protects: not only in refusal().
  const server = answering(() => new Response("x"));
  await assert.rejects(
    download("http://127.0.0.1:9/file", TOKEN, "text/plain", 100, { fetch: server.fetch }),
    { name: "DownloadFailed", message: /files\.slack\.com/ },
  );
  assert.equal(server.calls.length, 0);
});

test("a message takes at most five images and fifteen megabytes", () => {
  // the maintainer, 2026-09-25: images stay in the history and are sent again at every turn.
  const image = shared("image", { size: 1_000_000 });
  assert.equal(imagesRefusal(Array(IMAGES_PER_MESSAGE).fill(image)), null);
  assert.equal(
    imagesRefusal(Array(IMAGES_PER_MESSAGE + 1).fill(image)),
    texts.fill(texts.UPLOAD_TOO_MANY, {
      count: IMAGES_PER_MESSAGE + 1,
      limit: IMAGES_PER_MESSAGE,
    }),
  );
  const big = shared("image", { size: IMAGE_LIMIT });
  assert.equal(
    imagesRefusal([big, big, big]),
    texts.fill(texts.UPLOAD_TOO_HEAVY, {
      size: "21.5MB",
      limit: `${(IMAGES_LIMIT / 1024 / 1024).toFixed(1)}MB`,
    }),
  );
  // files are paths
  assert.equal(imagesRefusal(Array(9).fill(shared("snippet", { size: FILE_LIMIT }))), null);
});

test("saved files last three days", async (t) => {
  const folder = join(folderIn(t), "uploads");
  const old = await save(folder, { id: "F1", name: "old.txt" }, Buffer.from("x"));
  const fresh = await save(folder, { id: "F2", name: "fresh.txt" }, Buffer.from("x"));
  const stamp = Date.now() / 1000 - KEEP_SECONDS - 60;
  await utimes(old, stamp, stamp);
  await prepareUploads(folder);
  assert.ok(!existsSync(old) && existsSync(fresh)); // a resumed session still finds its files
  assert.equal(KEEP_SECONDS, 3 * 24 * 3600); // the maintainer, 2026-09-25
});

test("a folder that is not private is never written", WINDOWS, async (t) => {
  const root = folderIn(t);
  const folder = join(root, "uploads");
  mkdirSync(folder, { mode: 0o777 });
  chmodSync(folder, 0o777);
  await assert.rejects(save(folder, { id: "F1", name: "a.txt" }, Buffer.from("x")), {
    name: "DownloadFailed",
    message: /not private/,
  });
  const elsewhere = join(root, "elsewhere");
  mkdirSync(elsewhere, { mode: 0o700 });
  const link = join(root, "link");
  symlinkSync(elsewhere, link);
  await assert.rejects(save(link, { id: "F1", name: "a.txt" }, Buffer.from("x")), {
    name: "DownloadFailed",
    message: /not private/,
  });
});

const READABLE = [
  "text/plain",
  "text/markdown",
  "text/csv",
  "text/x-python",
  "application/pdf",
  "application/json",
  "application/x-yaml",
  "application/xml",
  "application/x-ipynb+json",
];
for (const mimetype of READABLE) {
  test(`common readable files pass [${mimetype}]`, () => {
    assert.equal(refusal(shared("snippet", { mimetype })), null);
  });
}

const OTHERS: [string, string][] = [
  ["application/zip", "application/zip"],
  ["application/octet-stream", "application/octet-stream"],
  ["application/x-msdownload", "application/x-msdownload"],
  [
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ],
  ["empty", ""],
];
for (const [name, mimetype] of OTHERS) {
  test(`other files are refused with their type [${name}]`, () => {
    // the maintainer, 2026-09-25: the most common files only, those Claude reads.
    const shown = mimetype || "unknown";
    assert.equal(
      refusal(shared("snippet", { mimetype })),
      texts.fill(texts.UPLOAD_FILE_TYPE, { mimetype: shown }),
    );
  });
}

test("a file that cannot be written says why", WINDOWS, async (t) => {
  const folder = join(folderIn(t), "uploads");
  mkdirSync(folder, { mode: 0o500 }); // private, but not writable
  await assert.rejects(save(folder, { id: "F1", name: "a.txt" }, Buffer.from("x")), {
    name: "DownloadFailed",
    message: /could not be saved/,
  });
});

test("a network failure keeps its detail", async () => {
  // What Node's fetch throws when the connection is refused: the owner sees why, not a class
  // name alone.
  const refused: FetchFn = async () => {
    throw new TypeError("fetch failed", { cause: new Error("connect ECONNREFUSED 127.0.0.1:1") });
  };
  await assert.rejects(download(URL_OF_FILE, TOKEN, "text/plain", 100, { fetch: refused }), {
    name: "DownloadFailed",
    message: /TypeError: fetch failed \(.*ECONNREFUSED.*\)/,
  });
});

test("a folder that is not private is logged at start", WINDOWS, async (t) => {
  const folder = join(folderIn(t), "uploads");
  mkdirSync(folder, { mode: 0o777 });
  chmodSync(folder, 0o777);
  const warnings: string[] = [];
  t.mock.method(logger, "warning", (message: string) => warnings.push(message));
  await prepareUploads(folder);
  assert.ok(warnings.some((message) => message.includes("not private")));
});

// Added for the TypeScript port: the cases `fetch` has and aiohttp's session did not.

test("a failure in the middle of the body is a failed download", async () => {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new Uint8Array(10));
      controller.error(new TypeError("terminated"));
    },
  });
  const server = answering(() => new Response(body, { headers: { "content-type": "text/plain" } }));
  await assert.rejects(download(URL_OF_FILE, TOKEN, "text/plain", 100, { fetch: server.fetch }), {
    name: "DownloadFailed",
    message: /TypeError: terminated/,
  });
});

test("a timeout is reported by its name", async () => {
  const timedOut: FetchFn = async () => {
    throw new DOMException("The operation was aborted due to timeout", "TimeoutError");
  };
  await assert.rejects(download(URL_OF_FILE, TOKEN, "text/plain", 100, { fetch: timedOut }), {
    name: "DownloadFailed",
    message: "TimeoutError",
  });
});

test("a cancel is not a failed download", async () => {
  const controller = new AbortController();
  const hanging: FetchFn = (_url, init) =>
    new Promise((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => reject(init.signal?.reason));
    });
  const pending = download(URL_OF_FILE, TOKEN, "text/plain", 100, {
    fetch: hanging,
    signal: controller.signal,
  });
  controller.abort();
  await assert.rejects(pending, (error: unknown) => !(error instanceof DownloadFailed));
});

test("a size shown in megabytes rounds a tie to even, as Python does", () => {
  // 0.25 MB is exact in binary: Python writes 0.2, `toFixed` would write 0.3.
  assert.equal(
    refusal(shared("image", { size: IMAGE_LIMIT + 1 })),
    texts.fill(texts.UPLOAD_IMAGE_SIZE, { size: "7.2MB", limit: "7.2MB" }),
  );
  assert.equal(
    refusal(shared("snippet", { size: FILE_LIMIT + 262144 })),
    texts.fill(texts.UPLOAD_FILE_SIZE, { size: "100.2MB", limit: "100.0MB" }),
  );
});
