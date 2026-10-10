/**
 * Does awaydesk still work on this @anthropic-ai/claude-agent-sdk release?
 *
 *     node probe/main.ts            run if the pinned SDK is not certified yet
 *     node probe/main.ts --force    run anyway
 *     node probe/main.ts --latest   run on the newest release on npm, in a temporary worktree
 *     node probe/main.ts --surface  check docs/sdk-surface-typescript.md alone and list its rows: no
 *                                   tokens
 *     node probe/main.ts --commands list the commands that are new or gone on this release: no tokens
 *
 * It uses the owner's Claude Code login and real tokens (Haiku), which is why it is not part of
 * the test suite. Exit status: 0 certified, 3 a claim is BROKEN, 2 not every gesture claim could
 * be proven; 1 is Node's own, for a crash.
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import {
  broken,
  type Certificate,
  CLAIMS,
  canCertify,
  certificate,
  checklist,
  evaluate,
  report,
} from "./claims.ts";
import { changes, report as commandsReport, offeredNow, recorded } from "./commands.ts";
import { byHand } from "./features.ts";
import { runScenes } from "./scenes.ts";
import {
  check as checkSurface,
  loadSurface,
  readReference,
  setAside,
  report as surfaceReport,
} from "./surface.ts";

const REPO = join(import.meta.dirname, "..");
const CERTIFIED = join(import.meta.dirname, "certified-versions.json");
const PACKAGE = "@anthropic-ai/claude-agent-sdk";
const REGISTRY = `https://registry.npmjs.org/${PACKAGE}/latest`;
const CERTIFICATE_KEYS = ["cli", "date", "holds", "open", "retired"];

function log(line: string): void {
  process.stderr.write(`${line}\n`);
}

type Certificates = Record<string, Certificate>;

/**
 * The certificates, by SDK version. Throws on a file of another shape, which a hand edit or an
 * interrupted write could leave: a certificate is never trusted on its looks.
 */
export function certified(path: string): Certificates {
  if (!existsSync(path)) return {};
  const known: unknown = JSON.parse(readFileSync(path, "utf8"));
  const name = path.split("/").at(-1);
  const entries =
    typeof known === "object" && known !== null && !Array.isArray(known)
      ? Object.values(known)
      : null;
  const shaped = entries?.every(
    (entry) =>
      typeof entry === "object" &&
      entry !== null &&
      Object.keys(entry).sort().join() === CERTIFICATE_KEYS.join(),
  );
  if (!shaped)
    throw new RangeError(`${name} is not a map of SDK versions to ${CERTIFICATE_KEYS.join(", ")}`);
  return known as Certificates;
}

/**
 * `packageJson` with the SDK pinned to `release`. Throws unless exactly one exact pin was
 * replaced: a pin written another way would leave the old SDK installed, and the probe would
 * certify a release it never ran.
 */
export function pinnedTo(packageJson: string, release: string): string {
  const pin = new RegExp(`("${PACKAGE.replace("/", "\\/")}"\\s*:\\s*")[0-9][0-9.]*"`, "g");
  const replaced = [...packageJson.matchAll(pin)].length;
  if (replaced !== 1) throw new RangeError(`expected one ${PACKAGE} exact pin, found ${replaced}`);
  return packageJson.replace(pin, `$1${release}"`);
}

/** The SDK and the Claude Code it bundles, as the package in `root` declares them. */
function installed(root: string): { sdk: string; cli: string } {
  const manifest: unknown = JSON.parse(
    readFileSync(join(root, "node_modules", PACKAGE, "package.json"), "utf8"),
  );
  const { version, claudeCodeVersion } = manifest as {
    version?: unknown;
    claudeCodeVersion?: unknown;
  };
  if (typeof version !== "string" || typeof claudeCodeVersion !== "string") {
    throw new RangeError(`${PACKAGE} declares no version or no claudeCodeVersion`);
  }
  return { sdk: version, cli: claudeCodeVersion };
}

/** The newest release on the npm registry. */
async function latestRelease(): Promise<string> {
  const response = await fetch(REGISTRY, { signal: AbortSignal.timeout(30_000) });
  if (!response.ok) throw new Error(`the npm registry answered ${response.status}`);
  const { version } = (await response.json()) as { version?: unknown };
  if (typeof version !== "string") throw new RangeError("the npm registry named no version");
  return version;
}

/** The environment of a git command: the variables a hook sets must not point it elsewhere. */
function gitEnv(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  for (const name of ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"]) delete env[name];
  return env;
}

/** Version numbers in order: `0.3.99` before `0.3.296`. */
function byVersion(a: string, b: string): number {
  const x = a.split(".").map(Number);
  const y = b.split(".").map(Number);
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
    const step = (x[i] ?? 0) - (y[i] ?? 0);
    if (step !== 0) return step;
  }
  return 0;
}

function sorted(known: Certificates): Certificates {
  return Object.fromEntries(Object.entries(known).sort(([a], [b]) => byVersion(a, b)));
}

/**
 * Runs the probe on the newest SDK in a detached worktree of HEAD, the way the release watch
 * moves the pin; the certificate is written to this checkout, the pin here is left alone.
 */
async function onLatest(force: boolean): Promise<number> {
  const latest = await latestRelease();
  const { sdk: pinned } = installed(REPO);
  if (latest === pinned) {
    log(`${PACKAGE} ${pinned} is the newest release`);
    return run(CERTIFIED, force);
  }
  log(`${PACKAGE} ${latest} is out (pinned ${pinned}); probing it in a temporary worktree`);
  const parent = mkdtempSync(join(tmpdir(), "awaydesk-probe-worktree-"));
  const tree = join(parent, "tree");
  const inherit = { stdio: "inherit" as const };
  try {
    const added = spawnSync("git", ["worktree", "add", "--detach", tree], {
      cwd: REPO,
      env: gitEnv(),
      ...inherit,
    });
    if (added.status !== 0) throw new Error("git worktree add failed");
    try {
      const install = spawnSync(
        "npm",
        ["install", "--save-exact", "--no-audit", "--no-fund", `${PACKAGE}@${latest}`],
        {
          cwd: tree,
          ...inherit,
        },
      );
      if (install.status !== 0) throw new Error("npm install failed");
      // The pin and the package in the tree are the release this run says it probes.
      const manifest = readFileSync(join(tree, "package.json"), "utf8");
      if (pinnedTo(manifest, latest) !== manifest || installed(tree).sdk !== latest) {
        throw new Error(`the worktree does not hold ${PACKAGE} ${latest}`);
      }
      const command = [
        join(tree, "probe", "main.ts"),
        "--certificate",
        CERTIFIED,
        ...(force ? ["--force"] : []),
      ];
      return spawnSync(process.execPath, command, { cwd: tree, ...inherit }).status ?? 1;
    } finally {
      // Not checked: a failed removal must not hide why the run above failed.
      const removed = spawnSync("git", ["worktree", "remove", "--force", tree], {
        cwd: REPO,
        env: gitEnv(),
      });
      if (removed.status !== 0) log(`could not remove the worktree: run \`git worktree prune\``);
    }
  } finally {
    rmSync(parent, { recursive: true, force: true });
  }
}

async function run(path: string, force: boolean): Promise<number> {
  const { sdk, cli } = installed(REPO);
  const known = certified(path);
  // Keyed by the SDK release: a new one can bundle a certified CLI and still change the
  // TypeScript side (its message types, `query`).
  if (known[sdk] && !force) {
    log(`${PACKAGE} ${sdk} (Claude Code ${cli}) is already certified`);
    return 0;
  }
  // Read before the scenes: a broken map stops the run before it spends tokens, and cannot
  // come between a finished run and its verdict.
  const left = byHand();
  // The SDK surface map, on the release this run is on: a symbol the package no longer defines
  // needs no scene to be known, so it stops the run here.
  const mapped = checkSurface(loadSurface(), await readReference(), setAside());
  console.log(`${surfaceReport(mapped)}\n`);
  if (mapped.broken) {
    console.log("Not certified: the package no longer defines a symbol the daemon uses.");
    return 3;
  }
  // The commands of this release against the recorded list: a new one is typed in Slack from
  // the day the SDK is pinned. Read before the scenes, like the surface, and printed after the
  // claims: it asks for a look, and stops no certificate.
  const commands = commandsReport(changes(await offeredNow(), recorded()), cli);
  const seen = await runScenes(log);
  const results = CLAIMS.map((claim) => evaluate(claim, seen[claim.id]));
  console.log(report(results, cli, sdk));
  console.log(`\n${commands}`);
  const hand = checklist(results);
  if (hand) console.log(`\n${hand}`);
  // What no claim covers at all, from the coverage map: printed on every run, certified or not.
  if (left) console.log(`\n${left}`);
  if (broken(results)) {
    console.log("\nNot certified: a claim is BROKEN. Do not pin this release.");
    return 3;
  }
  if (!canCertify(results)) {
    console.log("\nNot certified: a gesture claim could not be proven.");
    return 2;
  }
  known[sdk] = certificate(results, cli, new Date().toISOString().slice(0, 10));
  writeFileSync(path, `${JSON.stringify(sorted(known), null, 2)}\n`);
  console.log(`\nCertified: ${PACKAGE} ${sdk} added to ${path.split("/").at(-1)}.`);
  return 0;
}

/**
 * The SDK surface map against the installed release and the published reference, with the rows
 * a run only counts. It starts no session, so it spends nothing.
 */
async function showSurface(): Promise<number> {
  const mapped = checkSurface(loadSurface(), await readReference(), setAside());
  console.log(surfaceReport(mapped, true));
  return mapped.broken ? 3 : 0;
}

/**
 * The commands a session is offered on the installed release against the recorded list. It
 * starts Claude Code and sends no prompt, so it spends nothing.
 */
async function showCommands(): Promise<number> {
  console.log(commandsReport(changes(await offeredNow(), recorded()), installed(REPO).cli));
  return 0;
}

async function main(): Promise<number> {
  const { values } = parseArgs({
    options: {
      force: { type: "boolean", default: false },
      latest: { type: "boolean", default: false },
      surface: { type: "boolean", default: false },
      commands: { type: "boolean", default: false },
      // Where the certificate is kept: the run in a worktree writes to the checkout's file.
      certificate: { type: "string", default: CERTIFIED },
    },
  });
  if (values.surface) return showSurface();
  if (values.commands) return showCommands();
  return values.latest ? onLatest(values.force) : run(values.certificate, values.force);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exit(await main());
}
