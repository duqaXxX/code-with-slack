/**
 * What the probe claims about an `@anthropic-ai/claude-agent-sdk` release, and how an observation
 * becomes an outcome.
 *
 * The rules follow seedeep's upgrade guard: presence is conclusive, absence is not. A gesture claim
 * is one the probe causes itself, so once its event happened, the behaviour's absence is proof of a
 * break (BROKEN); if the event itself did not happen, nothing was learned (UNPROVEN). A model claim
 * needs Claude to act, so its failure proves nothing and is never reported as broken.
 */

export type Kind = "gesture" | "model";
export type Outcome = "HOLDS" | "BROKEN" | "UNPROVEN" | "RETIRED";

export interface Claim {
  readonly id: string;
  readonly kind: Kind;
  readonly text: string;
  /** Where to look when it breaks: `<file>#<export>` or `<file>#<class>.<member>`. */
  readonly guards: string;
  /** What to do in Slack to check it by hand when the probe could not prove it. */
  readonly how: string;
  /** The measurement, with date and version, that shows Claude Code no longer offers the event. */
  readonly retired: string | null;
}

/** `caused`: the event happened. `holds`: the behaviour the claim names was there. */
export interface Observation {
  readonly caused: boolean;
  readonly holds: boolean;
  readonly detail: string;
}

export interface Result {
  readonly claim: Claim;
  readonly outcome: Outcome;
  readonly detail: string;
}

/** A claim; throws unless its id reads P and a number and its kind is gesture or model. */
export function claim(
  id: string,
  kind: Kind,
  text: string,
  guards: string,
  how: string,
  retired: string | null = null,
): Claim {
  if (!/^P[1-9]\d*$/.test(id)) throw new RangeError(`a claim id reads P and a number, not ${id}`);
  if (kind !== "gesture" && kind !== "model") {
    throw new RangeError(`claim ${id}: kind ${kind} is neither gesture nor model`);
  }
  return Object.freeze({ id, kind, text, guards, how, retired });
}

/** What a scene saw; throws when it says a behaviour held although its event never happened. */
export function observation(caused: boolean, holds: boolean, detail = ""): Observation {
  if (holds && !caused)
    throw new RangeError("a behaviour cannot hold when its event never happened");
  return Object.freeze({ caused, holds, detail });
}

/** An outcome of a claim; a model claim is never BROKEN, whoever builds it. */
export function result(subject: Claim, outcome: Outcome, detail: string): Result {
  // The rule the whole probe rests on, kept by the type rather than by `evaluate` alone.
  if (outcome === "BROKEN" && subject.kind !== "gesture") {
    throw new RangeError(`claim ${subject.id} is a model claim: it is never BROKEN`);
  }
  return Object.freeze({ claim: subject, outcome, detail });
}

// The guards are the TypeScript back end's symbols that read what a claim names: the daemon's
// own code is where a break shows (the Python probe named the Python daemon's). The claims about
// the Agent SDK's own symbols name the TypeScript ones: the initialization result for Python's
// server info, `Options.effort` for `ClaudeAgentOptions(effort=...)`.
export const CLAIMS: readonly Claim[] = [
  claim(
    "P1",
    "gesture",
    "a session starts and its init message names the CLI version",
    "src/agent/claude/translate.ts#Translator.translate",
    "send any message in a bound channel; `!status` shows the Claude Code version",
  ),
  claim(
    "P2",
    "gesture",
    "`!status` reads the initialization result and the context usage",
    "src/agent/claude/session.ts#ClaudeSession.info",
    "send `!status`; it lists the model and a `Context:` line",
  ),
  claim(
    "P3",
    "gesture",
    "a text prompt gets a result whose session id is stored",
    "src/agent/claude/translate.ts#Translator.translate",
    "send a message; the reply closes with its footer",
  ),
  claim(
    "P4",
    "gesture",
    "an image prompt reaches Claude, which names the colour of the pixel",
    "src/agent/claude/prompt.ts#userMessage",
    "attach a screenshot and ask what it shows",
  ),
  claim(
    "P5",
    "model",
    "Claude reads an attached text file and quotes it",
    "src/chat/slack/attachments.ts#promptFor",
    "attach a small text file and ask what it says",
  ),
  claim(
    "P6",
    "gesture",
    "`!resume` knows the session: held by its thread, it is counted under the list",
    "src/agent/claude/listing.ts#directorySessions",
    "send `!resume`; the line under the list counts the session open in its own thread",
  ),
  claim(
    "P7",
    "gesture",
    "a resumed session remembers what was said before it",
    "src/agent/claude/backend.ts#ClaudeBackend.start",
    "press Resume on an older session and ask what you were talking about",
  ),
  claim(
    "P8",
    "gesture",
    "`!stop` interrupts a running turn and its reply ends",
    "src/agent/claude/session.ts#ClaudeSession.interrupt",
    "ask for a long answer and send `!stop` while it writes",
  ),
  claim(
    "P9",
    "model",
    "with `!bypass on`, a Bash call runs without asking",
    "src/agent/claude/session.ts#ClaudeSession.setPermissionMode",
    "send `!bypass on`, ask Claude to run a command: no Approve button; then `!bypass off`",
  ),
  claim(
    "P10",
    "model",
    "a Bash call shows its task card in the reply",
    "src/agent/claude/translate.ts#Translator.translate",
    "ask Claude to run `echo hello` with Bash",
  ),
  claim(
    "P11",
    "model",
    "a Bash call asks for approval, and an approved call runs",
    "src/agent/claude/requests.ts#toRequest",
    "with bypass off, ask Claude to run a command; press Approve",
  ),
  claim(
    "P12",
    "model",
    "`!stop` ends a background command",
    "src/agent/claude/session.ts#ClaudeSession.stopTask",
    "ask Claude to run `tail -f` on a file in the background, then send `!stop`",
  ),
  claim(
    "P13",
    "model",
    "a Write and an Edit show the terminal's preview (undocumented tool_use_result)",
    "src/agent/claude/translate.ts#fileChange",
    "ask Claude to create a file and then edit it; each call shows as one container, titled " +
      "with its line, and no card",
  ),
  claim(
    "P14",
    "model",
    "a hook's `cwd` follows a `cd`, so the footer shows the branch of a trusted repo there",
    "src/agent/claude/hooks.ts#postToolUseHookEvents",
    "bind a folder holding a repo you trusted in Claude Code one level down, ask Claude to " +
      "`cd` into it; the footer shows the repo's branch",
  ),
  claim(
    "P15",
    "gesture",
    "a resumed session keeps the model set with `/model`",
    "src/agent/claude/backend.ts#ClaudeBackend.start",
    "in a thread, send `!model sonnet`, let the session idle-close or restart the daemon, " +
      "send a message; `!status` shows the model",
  ),
  claim(
    "P16",
    "gesture",
    "a resumed session loses the effort set with `setEffort`, and `Options.effort` restores it",
    "src/agent/claude/session.ts#ClaudeSession.setEffort",
    "in a thread, send `!effort low`, restart the daemon, send a message; the footer shows `low`",
  ),
  claim(
    "P17",
    "gesture",
    "the initialization result lists models with `value`, `displayName` and " +
      "`supportedEffortLevels`",
    "src/agent/claude/info.ts#agentInfo",
    "send a message at the top level of a bound channel; its thread offers Model and Effort " +
      "with the CLI's own names",
  ),
  claim(
    "P18",
    "gesture",
    "a resumed session keeps the model set with `setModel()`",
    "src/agent/claude/session.ts#ClaudeSession.setModel",
    "at the top level, pick a model in the setup and press Start, restart the daemon, reply " +
      "in the thread; `!status` shows the model",
  ),
  claim(
    "P19",
    "gesture",
    "a prompt sent under a uuid is re-emitted with it (`--replay-user-messages`), before the " +
      "first stream event of its turn",
    "src/agent/claude/translate.ts#Translator.promptSent",
    "send a message while Claude Code reports a background task, as `docs/features.md` " +
      "describes: the reply ends with the note, or the session answers it in a turn of its own",
  ),
  claim(
    "P20",
    "gesture",
    "the reply to `/goal` opens with the command's `Goal set:` line: an assistant message " +
      "whose `message_id` no `message_start` event announced",
    "src/agent/claude/translate.ts#Translator.translate",
    "send `!goal reply with the single word tick`; the reply opens with `Goal set:` and the " +
      "condition, then Claude's text, written once",
  ),
  claim(
    "P21",
    "gesture",
    "the reply to `/compact` is the compaction's line: its `compact_boundary` comes before " +
      "every frame of its turn that starts one, and a result follows it",
    "src/agent/claude/translate.ts#Translator.translate",
    "send `!compact` in a thread with a few turns; the reply is `Compacted the " +
      "conversation:` and the tokens before and after",
  ),
  claim(
    "P22",
    "gesture",
    "`/compact` opens with a `status` system message that says `compacting`: the thread's " +
      "status line reads `Compacting conversation…` while it runs",
    "src/agent/claude/translate.ts#Translator.translate",
    "send `!compact` in a thread with a few turns; the line under the thread reads " +
      "`Compacting conversation…` until the reply appears",
  ),
  claim(
    "P23",
    "gesture",
    "each command `docs/limits.md` lists as not offered to a session is answered `isn't " +
      "available in this environment`",
    "src/core/commands.ts#parseBang",
    "send `!rewind` in a thread; the reply is `/rewind isn't available in this " +
      "environment.` A command that answers anything else is no longer a limit: take its " +
      "row out of `docs/limits.md` and say so in the issue it names",
  ),
];

if (new Set(CLAIMS.map((c) => c.id)).size !== CLAIMS.length) {
  throw new RangeError("two claims share an id");
}

/** The outcome of `subject` given what its scene saw (null: the scene never ran). */
export function evaluate(subject: Claim, seen: Observation | null | undefined): Result {
  if (subject.retired !== null) return result(subject, "RETIRED", subject.retired);
  if (seen === null || seen === undefined || !seen.caused) {
    return result(subject, "UNPROVEN", seen ? seen.detail : "not attempted");
  }
  if (seen.holds) return result(subject, "HOLDS", seen.detail);
  return result(subject, subject.kind === "gesture" ? "BROKEN" : "UNPROVEN", seen.detail);
}

/** Every gesture claim holds or is retired: a run that proved nothing certifies nothing. */
export function canCertify(results: readonly Result[]): boolean {
  return results
    .filter((r) => r.claim.kind === "gesture")
    .every((r) => r.outcome === "HOLDS" || r.outcome === "RETIRED");
}

export function broken(results: readonly Result[]): boolean {
  return results.some((r) => r.outcome === "BROKEN");
}

export function report(results: readonly Result[], cliVersion: string, sdkVersion: string): string {
  const lines = [
    `@anthropic-ai/claude-agent-sdk ${sdkVersion}, bundled Claude Code ${cliVersion}`,
    "",
  ];
  for (const r of results) {
    const detail = r.detail ? `  (${r.detail})` : "";
    lines.push(
      `${r.outcome.padEnd(8)} ${r.claim.id.padEnd(4)} ${r.claim.kind.padEnd(7)} ${r.claim.text}${detail}`,
    );
  }
  return lines.join("\n");
}

/** What to test by hand: the claims the run could not prove, and where a break would be. */
export function checklist(results: readonly Result[]): string {
  const open = results.filter((r) => r.outcome === "UNPROVEN" || r.outcome === "BROKEN");
  if (open.length === 0) return "";
  const lines = ["Test these by hand in Slack; the probe could not prove them:", ""];
  for (const r of open) {
    lines.push(
      `  [ ] ${r.claim.id} ${r.claim.text}`,
      `      how: ${r.claim.how}`,
      `      if it fails, look at ${r.claim.guards}`,
    );
  }
  return lines.join("\n");
}

export interface Certificate {
  readonly cli: string;
  readonly date: string;
  readonly holds: string[];
  readonly open: string[];
  readonly retired: string[];
}

/** The entry `certified-versions.json` keeps for a release: ids and versions, no content. */
export function certificate(
  results: readonly Result[],
  cliVersion: string,
  date: string,
): Certificate {
  const ids = (outcome: Outcome) =>
    results.filter((r) => r.outcome === outcome).map((r) => r.claim.id);
  return {
    cli: cliVersion,
    date,
    holds: ids("HOLDS"),
    open: ids("UNPROVEN"),
    retired: ids("RETIRED"),
  };
}
