/**
 * The coverage map in `docs/features.md`: which probe claims each feature has, and what is left
 * to check by hand. The probe prints the second at the end of a run;
 * `test/probe/features.test.ts` keeps the first in step with `CLAIMS`.
 */
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(import.meta.dirname, "..");
export const FEATURES = join(ROOT, "docs", "features.md");
const COLUMNS = ["Feature", "Test suite", "Probe", "By hand"];

export interface Feature {
  readonly name: string;
  /** Test modules, as `test_sessions`, or the path of a `*.test.ts` file. */
  readonly tests: readonly string[];
  /** Probe claim ids, as `P1`. */
  readonly claims: readonly string[];
  /** Empty when nothing is left to check by hand. */
  readonly byHand: string;
}

/** The cells of a table row, without the bars at its ends. */
function cellsOf(row: string): string[] {
  return row
    .replace(/^\|+|\|+$/g, "")
    .split("|")
    .map((cell) => cell.trim());
}

/**
 * The table's rows. Throws when the table is missing, its header changed, or a row does not have
 * one cell per column: a broken row is an error, never a row left out of the checks and the
 * checklist.
 */
export function features(path: string = FEATURES): Feature[] {
  const rows = readFileSync(path, "utf8")
    .split(/\r?\n/)
    .filter((line) => line.startsWith("|"));
  const cells = rows.map(cellsOf);
  const header = cells[0];
  if (header === undefined || header.join("\0") !== COLUMNS.join("\0")) {
    throw new RangeError(`${basename(path)}: no table with the columns ${COLUMNS.join(", ")}`);
  }
  const out: Feature[] = [];
  for (const row of cells.slice(2)) {
    if (row.length !== COLUMNS.length) {
      // A `|` inside a cell splits it too: write it another way.
      throw new RangeError(
        `${basename(path)}: ${row.length} cells, not ${COLUMNS.length}, in ${row[0] ?? ""}`,
      );
    }
    const [name = "", tests = "", claims = "", byHand = ""] = row;
    out.push({
      name,
      tests: [...tests.matchAll(/`(test_\w+|[\w./-]+\.test\.ts)`/g)].map((m) => m[1] ?? ""),
      claims: [...claims.matchAll(/\bP\d+\b/g)].map((m) => m[0]),
      byHand: byHand === "none" ? "" : byHand,
    });
  }
  return out;
}

function basename(path: string): string {
  return path.split("/").at(-1) ?? path;
}

function testFiles(directory: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...testFiles(path));
    else if (entry.name.endsWith(".test.ts")) found.push(path);
  }
  return found;
}

/**
 * Whether a test module the table names exists: the path of a `*.test.ts` file, or a Python
 * module name (`test_state`), which is `tests/test_state.py` while the Python tree stands and a
 * `state.test.ts` file anywhere under `test/` once the suite is ported (underscores become
 * hyphens).
 */
export function testModuleExists(module: string): boolean {
  if (module.endsWith(".test.ts")) return existsSync(join(ROOT, module));
  if (existsSync(join(ROOT, "tests", `${module}.py`))) return true;
  const wanted = `${module.replace(/^test_/, "").replaceAll("_", "-")}.test.ts`;
  return testFiles(join(ROOT, "test")).some((path) => basename(path) === wanted);
}

/** The checks no test and no probe claim makes, as the probe prints them. */
export function byHand(path: string = FEATURES): string {
  const left = features(path).filter((f) => f.byHand);
  if (left.length === 0) return "";
  const lines = [
    "Checked by nothing automatic (docs/features.md), when the feature or Slack changes:",
    "",
  ];
  for (const f of left) lines.push(`  [ ] ${f.name}\n      how: ${f.byHand}`);
  return lines.join("\n");
}
