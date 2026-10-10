/**
 * The recordings the tests replay. They stay under `tests/fixtures/`, where the Python suite
 * reads them, until the Python tree is removed.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(import.meta.dirname, "..", "..");
export const FIXTURES = join(ROOT, "tests", "fixtures");
export const GOLDEN = join(ROOT, "test", "golden");

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

function names(directory: string, extension: string): string[] {
  return readdirSync(directory)
    .filter((file) => file.endsWith(extension))
    .map((file) => file.slice(0, -extension.length))
    .sort();
}

/** The names of the recorded SDK streams. */
export function sdkRecordings(): string[] {
  return names(join(FIXTURES, "sdk"), ".jsonl");
}

/** A recorded SDK stream: the CLI's wire records, one per line, as they were written. */
export function sdkRecords(name: string): JsonObject[] {
  return readFileSync(join(FIXTURES, "sdk", `${name}.jsonl`), "utf8")
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => JSON.parse(line) as JsonObject);
}

export function sdkJson(name: string): Json {
  return JSON.parse(readFileSync(join(FIXTURES, "sdk", `${name}.json`), "utf8")) as Json;
}

/** The names of the recorded Slack payloads. */
export function slackPayloads(): string[] {
  return names(join(FIXTURES, "slack"), ".json");
}

export function slackPayload(name: string): JsonObject {
  return JSON.parse(readFileSync(join(FIXTURES, "slack", `${name}.json`), "utf8")) as JsonObject;
}

/** What the Python code produced at `seam` for a recording. */
export function golden(seam: "reply" | "slack", name: string): JsonObject {
  return JSON.parse(readFileSync(join(GOLDEN, seam, `${name}.json`), "utf8")) as JsonObject;
}
