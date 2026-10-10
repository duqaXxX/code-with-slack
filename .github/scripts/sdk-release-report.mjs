/**
 * The text of the SDK release watch's issue, from the versions and the test outcome.
 *
 * awaydesk pins `@anthropic-ai/claude-agent-sdk` and runs the Claude Code CLI the SDK bundles,
 * whose message shapes the fixtures record. A new SDK release is read against the project here:
 * the workflow installs it, runs the suite, and this names what is left to check by hand.
 *
 *     PINNED=0.3.296 LATEST=0.3.298 LATEST_CLI=2.1.298 FIXTURE_CLI=2.1.296 OUTCOME=pass \
 *     LAST_COMMENTED=0.3.297 node .github/scripts/sdk-release-report.mjs
 *
 * prints `{"title": ..., "body": ..., "comment": ... or null}` as JSON, or `null` when the latest
 * release is the pinned one and there is nothing to report.
 *
 * The workflow runs this beside the token that writes the issue, in a job that never installs the
 * project, so it runs on the runner's own `node`: plain JavaScript (no TypeScript syntax, which
 * only Node 22.18 and later strips) importing Node's standard library only, nothing from the
 * project and no lookup in its `package.json`.
 */
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

const VERSION = /^\d+(\.\d+)+$/;
// The words the workflow's test job writes, and the default below for a run that names none.
const OUTCOMES = new Set(["pass", "fail", "install failed", "skipped"]);

/**
 * @typedef {object} ReleaseReport
 * @property {string} title
 * @property {string} body
 * @property {string | null} comment
 */

/**
 * @param {string} version
 * @returns {string}
 */
function checked(version) {
  // Versions reach an issue title and a shell command: anything else is refused.
  if (!VERSION.test(version)) throw new Error(`not a version: ${JSON.stringify(version)}`);
  return version;
}

/**
 * @param {string} outcome
 * @returns {string}
 */
function known(outcome) {
  // The outcome reaches the issue's body and a comment, and comes from the job that ran the
  // release under test: anything but these words is refused.
  if (!OUTCOMES.has(outcome)) throw new Error(`not an outcome: ${JSON.stringify(outcome)}`);
  return outcome;
}

/**
 * The issue for `latest`, or null when it is the pinned version. A comment (a notification)
 * comes once per new version, and at every run whose suite did not pass.
 *
 * @param {object} run
 * @param {string} run.pinned
 * @param {string} run.latest
 * @param {string} run.latestCli
 * @param {string} run.fixtureCli
 * @param {string} run.outcome
 * @param {string} run.lastCommented
 * @returns {ReleaseReport | null}
 */
export function report({ pinned, latest, latestCli, fixtureCli, outcome, lastCommented }) {
  checked(pinned);
  checked(latest);
  checked(latestCli);
  checked(fixtureCli);
  known(outcome);
  if (latest === pinned) return null;
  const cliChanged = latestCli !== fixtureCli;
  const steps = [
    "- [ ] On the owner's machine, `node probe/main.ts --latest` certifies the release; " +
      "work through its hand checklist, if it prints one. Never pin a release with a BROKEN " +
      "claim.",
    "- [ ] Merge the Dependabot pull request that moves the pin, or move it by hand.",
    "- [ ] `npm test`, `npm run typecheck`, `npm run lint` pass on the new pin.",
  ];
  if (cliChanged) {
    steps.push(
      `- [ ] The bundled CLI moved from ${fixtureCli} to ${latestCli}: re-record the SDK ` +
        "streams the fixtures hold and compare their shapes.",
    );
  }
  const body = [
    `\`@anthropic-ai/claude-agent-sdk\` ${latest} is on npm; awaydesk pins ${pinned}.`,
    "",
    "| | Version |",
    "|---|---|",
    `| Pinned SDK | ${pinned} |`,
    `| Latest SDK | ${latest} |`,
    `| CLI bundled with it | ${latestCli} |`,
    `| CLI the fixtures record | ${fixtureCli} |`,
    `| Test suite on ${latest} | ${outcome} |`,
    "",
    ...steps,
    "",
    "This body is rewritten by the SDK release watch workflow. Close the issue once the " +
      "release is checked; a closed issue with this title is not opened again.",
  ].join("\n");
  /** @type {string | null} */
  let comment = null;
  if (outcome !== "pass") {
    comment = `The test suite on @anthropic-ai/claude-agent-sdk ${latest}: ${outcome}.`;
  } else if (latest !== lastCommented) {
    comment = `@anthropic-ai/claude-agent-sdk ${latest} is out (bundled CLI ${latestCli}); tests: ${outcome}.`;
  }
  return {
    title: `@anthropic-ai/claude-agent-sdk ${latest}: test awaydesk against it`,
    body,
    comment,
  };
}

/**
 * @param {string} name
 * @returns {string}
 */
function required(name) {
  const value = process.env[name];
  if (value === undefined) throw new Error(`${name} is required`);
  return value;
}

// Run only as the entry point, so a test can import `report`.
const entry = process.argv[1];
if (entry !== undefined && realpathSync(entry) === fileURLToPath(import.meta.url)) {
  console.log(
    JSON.stringify(
      report({
        pinned: required("PINNED"),
        latest: required("LATEST"),
        latestCli: required("LATEST_CLI"),
        fixtureCli: required("FIXTURE_CLI"),
        outcome: process.env.OUTCOME ?? "skipped",
        lastCommented: process.env.LAST_COMMENTED ?? "",
      }),
    ),
  );
}
