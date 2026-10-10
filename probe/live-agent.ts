/**
 * The live check of the Claude back end: real sessions through `ClaudeBackend` and
 * `ClaudeSession` only, on `haiku`, with the owner's Claude Code login (no API key). Run from the
 * repository root:
 *
 *     node probe/live-agent.ts
 *
 * Each session works in a fresh temporary folder and takes no setting source, so nothing of the
 * owner's settings, hooks or permission rules is in play. Prompts are a few words and the whole
 * run is a few cents of tokens. One line per check, `PASS` or `FAIL` and what it saw; `INFO` lines
 * settle the questions the spike and the translation left open. Exits 1 when a check fails.
 *
 * A reply's text is never printed beyond a few characters of evidence, and no path is printed.
 */
import { randomUUID } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { ClaudeBackend } from "../src/agent/claude/backend.ts";
import {
  LONG_PROJECT_KEY,
  projectFolder,
  projectKey,
  projectsDir,
} from "../src/agent/claude/listing.ts";
import { ResumeRefused } from "../src/agent/claude/session.ts";
import type {
  AgentSession,
  PermissionAnswer,
  PermissionRequest,
  QuestionAnswer,
  QuestionRequest,
  RequestHandler,
  SessionEvent,
  StartOptions,
} from "../src/agent/seam.ts";

const TURN_MS = 120_000;
const MODEL = "haiku";
const CODEWORD = "PELICAN-4417";

let failures = 0;
let root = "";
// The folders the sessions worked in: Claude Code keeps their transcripts under the owner's
// config directory, which the run removes again.
const worked: string[] = [];

/** `text` without any path of this machine, cut to `limit` characters. */
function scrub(text: string, limit = 90): string {
  const clean = text.split(root).join("<tmp>").split(homedir()).join("<home>").replace(/\s+/g, " ");
  return clean.length > limit ? `${clean.slice(0, limit)}...` : clean;
}

function report(ok: boolean, name: string, saw: string): void {
  if (!ok) failures += 1;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}: ${scrub(saw, 160)}`);
}

function info(question: string, answer: string): void {
  console.log(`INFO ${question}: ${scrub(answer, 700)}`);
}

/** Records a session's events and waits for what a check needs. */
class Recorder {
  readonly events: SessionEvent[] = [];
  ended = false;
  readonly #wake: (() => void)[] = [];

  constructor(session: AgentSession) {
    void (async () => {
      for await (const event of session.events) {
        this.events.push(event);
        this.#poke();
      }
      this.ended = true;
      this.#poke();
    })();
  }

  #poke(): void {
    for (const wake of this.#wake.splice(0)) wake();
  }

  /** The events from `from` on, once `done` holds of them, the stream ends or `ms` pass. */
  async wait(
    from: number,
    done: (events: SessionEvent[]) => boolean,
    ms: number = TURN_MS,
  ): Promise<SessionEvent[]> {
    const deadline = Date.now() + ms;
    for (;;) {
      const slice = this.events.slice(from);
      const left = deadline - Date.now();
      if (done(slice) || this.ended || left <= 0) return slice;
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, left);
        this.#wake.push(() => {
          clearTimeout(timer);
          resolve();
        });
      });
    }
  }
}

/** The owner's side: answers what the session asks as the current check needs. */
class Owner implements RequestHandler {
  readonly permissions: PermissionRequest[] = [];
  readonly questions: QuestionRequest[] = [];
  onPermission: (request: PermissionRequest) => PermissionAnswer = () => ({ allow: true });
  onQuestion: (request: QuestionRequest) => QuestionAnswer = () => ({
    answered: false,
    message: "skipped",
  });

  async permission(request: PermissionRequest): Promise<PermissionAnswer> {
    this.permissions.push(request);
    return this.onPermission(request);
  }

  async question(request: QuestionRequest): Promise<QuestionAnswer> {
    this.questions.push(request);
    return this.onQuestion(request);
  }
}

async function folder(name: string): Promise<string> {
  const path = join(root, name);
  await mkdir(path, { recursive: true });
  return path;
}

/** Removes the transcripts of the folders this run worked in, and only those. */
async function removeTranscripts(): Promise<void> {
  for (const path of worked) {
    const found = await projectFolder(path, projectsDir());
    // The name holds the run's random folder: nothing of the owner's can match.
    if (found === null || !found.includes("awaydesk-live-")) continue;
    await rm(found, { recursive: true, force: true });
  }
}

function options(directory: string, resume: string | null = null): StartOptions {
  return {
    folder: directory,
    resume,
    settingsSources: [],
    model: MODEL,
    effort: null,
    permissionMode: null,
  };
}

/** Sends `content` and returns the events up to the end of the turn it starts. */
async function turn(
  session: AgentSession,
  recorder: Recorder,
  content: string,
  until: (events: SessionEvent[]) => boolean = (events) =>
    events.some((event) => event.type === "turn_ended"),
): Promise<{ id: string; events: SessionEvent[] }> {
  const from = recorder.events.length;
  const id = randomUUID();
  await session.send({ id, content });
  return { id, events: await recorder.wait(from, until) };
}

function ofType<T extends SessionEvent["type"]>(
  events: readonly SessionEvent[],
  type: T,
): Extract<SessionEvent, { type: T }>[] {
  return events.filter((event): event is Extract<SessionEvent, { type: T }> => event.type === type);
}

function finalTextOf(events: readonly SessionEvent[]): string {
  return ofType(events, "turn_ended").at(-1)?.finalText ?? "";
}

/** A check that must not stop the others when it throws. */
async function section(name: string, run: () => Promise<void>): Promise<void> {
  try {
    await run();
  } catch (error) {
    report(false, name, `threw ${error instanceof Error ? error.name : typeof error}`);
  }
}

async function main(): Promise<void> {
  root = await mkdtemp(join(tmpdir(), "awaydesk-live-"));
  // The owner's own `~/.claude.json` is not read: Chrome stays off for these sessions.
  const home = await folder("home");
  await writeFile(join(home, ".claude.json"), "{}");
  const backend = new ClaudeBackend({ home });
  const owner = new Owner();
  const sessions: AgentSession[] = [];
  const directory = await folder("project");
  let sessionId: string | null = null;

  async function open(
    at: string,
    resume: string | null = null,
  ): Promise<{ session: AgentSession; recorder: Recorder; startedIn: number }> {
    const began = Date.now();
    worked.push(at);
    const session = await backend.start(options(at, resume), owner);
    sessions.push(session);
    return { session, recorder: new Recorder(session), startedIn: Date.now() - began };
  }

  try {
    // 1. A session starts, answers a prompt.
    const primary = await open(directory);
    const { session, recorder } = primary;
    report(true, "start", `the back end returned a session in ${primary.startedIn} ms`);

    await section("first turn", async () => {
      const first = await turn(
        session,
        recorder,
        `Remember the codeword ${CODEWORD}. Reply with the single word ok.`,
      );
      const started = ofType(first.events, "session_started");
      sessionId = started[0]?.sessionId ?? null;
      report(
        sessionId !== null && started[0]?.agentVersion !== null,
        "session_started carries an id and a version",
        `id ${sessionId === null ? "none" : "present"}, version ${started[0]?.agentVersion ?? "none"}`,
      );
      const deltas = ofType(first.events, "text_delta");
      const ends = ofType(first.events, "turn_ended");
      report(
        deltas.length > 0 && ends.length === 1 && ends[0]?.ending === "done",
        "a prompt gives text_deltas and one turn_ended",
        `${deltas.length} text_delta, ${ends.length} turn_ended (${ends[0]?.ending ?? "none"}), tokens by ${Object.keys(ends[0]?.tokens ?? {}).length} model`,
      );
      const taken = ofType(first.events, "prompt_taken");
      report(
        taken.length === 1 && taken[0]?.promptId === first.id,
        "prompt_taken names the prompt's id",
        `${taken.length} prompt_taken, id ${taken[0]?.promptId === first.id ? "matches" : "differs"}`,
      );
    });

    // 2. What the session says of itself.
    await section("info", async () => {
      const about = await session.info();
      const aliased = about.commands.filter((command) => command.aliases.length > 0);
      report(
        about.models.length > 0 && about.commands.length > 0,
        "info() has models and commands",
        `${about.models.length} models, ${about.commands.length} commands, mode ${about.permissionMode ?? "none"}`,
      );
      info(
        "do info().commands carry aliases (source: the initialization result)",
        `${aliased.length} of ${about.commands.length} carry aliases, e.g. ${aliased[0]?.name ?? "none"} = ${aliased[0]?.aliases.join(",") ?? ""}`,
      );
      info(
        "does the initialization result carry the current permission mode",
        `${about.permissionMode === null ? "no" : `yes, ${about.permissionMode}`}`,
      );
    });

    await section("context usage", async () => {
      const usage = await session.contextUsage();
      report(
        usage.model !== null && usage.percentage !== null,
        "contextUsage() gives the model and the share",
        `model ${usage.model ?? "none"}, ${usage.percentage ?? "none"} percent`,
      );
    });

    // 3. A tool call, a permission request, an allow with a changed input. Claude Code runs a
    // read-only command (`echo`) without asking, so the tool is `Write`.
    await section("allow with a changed input", async () => {
      owner.permissions.length = 0;
      owner.onPermission = (request) =>
        request.toolName === "Write"
          ? { allow: true, changedInput: { ...request.input, content: "CHANGED-by-owner-31" } }
          : { allow: true };
      const run = await turn(
        session,
        recorder,
        "Use the Write tool to create the file note.txt with exactly this content: ORIGINAL-by-model-31. Then reply with the single word done.",
      );
      const started = ofType(run.events, "call_started");
      const ended = ofType(run.events, "call_ended");
      report(
        started.length > 0 && ended.length > 0 && started[0]?.callId === ended[0]?.callId,
        "a tool call gives call_started and call_ended",
        `${started.length} call_started (${started[0]?.toolName ?? "none"}), ${ended.length} call_ended, file change ${ended[0]?.fileChange === null ? "none" : "reported"}`,
      );
      const asked = owner.permissions[0];
      report(
        asked !== undefined &&
          asked.toolName === "Write" &&
          typeof asked.input.file_path === "string" &&
          asked.callId !== null,
        "a permission request reaches the handler with a tool name and an input",
        asked === undefined
          ? "no request reached the handler"
          : `${owner.permissions.length} request, tool ${asked.toolName}, call id ${asked.callId === null ? "none" : "present"}, title ${asked.title === null ? "none" : "present"}, description ${asked.description === null ? "none" : "present"}`,
      );
      const written = await readFile(join(directory, "note.txt"), "utf8").catch(() => "");
      report(
        written.includes("CHANGED-by-owner-31") && !written.includes("ORIGINAL-by-model-31"),
        "an allow with a changed input changes what the tool did",
        `the file holds the owner's text: ${written.includes("CHANGED-by-owner-31")}, the model's: ${written.includes("ORIGINAL-by-model-31")}`,
      );
    });

    // 4. A deny.
    await section("deny", async () => {
      owner.onPermission = () => ({
        allow: false,
        message: "The owner refused this command, reason code 7731.",
      });
      const run = await turn(
        session,
        recorder,
        "Use the Write tool to create the file refused.txt with the content hello. If it is refused, reply with the reason code you were given and nothing else.",
      );
      const ended = ofType(run.events, "call_ended");
      const exists = await readFile(join(directory, "refused.txt"), "utf8").then(
        () => true,
        () => false,
      );
      const said = `${finalTextOf(run.events)} ${ofType(run.events, "text_delta")
        .map((event) => event.text)
        .join("")}`;
      report(
        ended.some((event) => event.isError) && said.includes("7731") && !exists,
        "a deny's message reaches the model",
        `call_ended with an error: ${ended.some((event) => event.isError)}, the reply repeats the code: ${said.includes("7731")}, the file was written: ${exists}`,
      );
      owner.onPermission = () => ({ allow: true });
    });

    // 5. Effort and permission mode.
    await section("effort", async () => {
      const began = Date.now();
      await session.setEffort("low");
      const took = Date.now() - began;
      const run = await turn(session, recorder, "Reply with the single word ok.");
      const seen = ofType(run.events, "effort_observed").at(-1);
      report(
        seen?.level === "low",
        "setEffort then the next effort_observed",
        `setEffort took ${took} ms, the next Stop hook reported ${seen === undefined ? "nothing" : (seen.level ?? "none")}`,
      );
      const folders = ofType(run.events, "folder_changed");
      info("does the Stop hook report the folder", `${folders.length} folder_changed in the turn`);
    });

    await section("permission mode", async () => {
      const from = recorder.events.length;
      await session.setPermissionMode("acceptEdits");
      const seen = await recorder.wait(
        from,
        (events) => events.some((event) => event.type === "mode_changed"),
        15_000,
      );
      const changed = ofType(seen, "mode_changed")[0];
      report(
        changed?.mode === "acceptEdits",
        "setPermissionMode then mode_changed",
        `mode_changed ${changed?.mode ?? "never came"}`,
      );
      await session.setPermissionMode("default");
    });

    // 6. The listing.
    await section("listing", async () => {
      const listed = await backend.listSessions(directory);
      const found = listed.find((entry) => entry.id === sessionId);
      report(
        found !== undefined,
        "listSessions lists the session just made",
        `${listed.length} listed, title ${found === undefined ? "none" : "present"}, size ${found?.size ?? "none"}`,
      );
      const dated = await backend.datedSessions(directory, listed);
      const alive = await backend.aliveSessions(directory);
      report(
        dated.some((entry) => entry.id === sessionId) && alive?.has(sessionId ?? "") === true,
        "the session is dated and alive",
        `${dated.length} dated, alive set ${alive === null ? "cannot tell" : `has ${alive.size}`}`,
      );
    });

    // 7. Interrupt.
    await section("interrupt", async () => {
      const from = recorder.events.length;
      await session.send({
        id: randomUUID(),
        content: "Count from 1 to 400, one number per line, with no other text.",
      });
      await recorder.wait(
        from,
        (events) => events.some((event) => event.type === "text_delta"),
        60_000,
      );
      await session.interrupt();
      const seen = await recorder.wait(from, (events) =>
        events.some((event) => event.type === "turn_ended"),
      );
      const ended = ofType(seen, "turn_ended")[0];
      report(
        ended?.ending === "interrupted",
        "interrupt ends a turn as interrupted",
        `turn_ended ${ended?.ending ?? "never came"}`,
      );
    });

    // 8. A background command that ends after its turn.
    await section("background task", async () => {
      const from = recorder.events.length;
      await session.send({
        id: randomUUID(),
        content:
          "Run this with the Bash tool and run_in_background set to true: sleep 4 && echo late. Then reply with the single word STARTED and end your turn without waiting.",
      });
      const seen = await recorder.wait(
        from,
        (events) =>
          ofType(events, "turn_ended").length >= 2 ||
          (events.some((e) => e.type === "task_ended") &&
            ofType(events, "turn_ended").some((e) => e.startedBy === "agent")),
        TURN_MS,
      );
      const started = ofType(seen, "task_started");
      const closing = seen.filter(
        (event) => event.type === "task_updated" || event.type === "task_ended",
      );
      const turns = ofType(seen, "turn_ended");
      const agentTurn = turns.find((event) => event.startedBy === "agent");
      report(
        started.length > 0 && closing.length > 0 && agentTurn !== undefined,
        "a background command gives task_started, task_updated or task_ended, then an agent turn",
        `task_started ${started.length} (kind ${started[0]?.kind ?? "none"}, type ${started[0]?.taskType ?? "none"}), task_updated ${ofType(seen, "task_updated").length}, task_ended ${ofType(seen, "task_ended").length}, turns by ${turns.map((event) => event.startedBy).join(",")}`,
      );
    });

    // 9. A subagent.
    await section("subagent", async () => {
      // A session of its own: the shape of a subagent's records is read from a clean start.
      const fresh = await open(await folder("subagent"));
      const run = await turn(
        fresh.session,
        fresh.recorder,
        "Use the Agent tool exactly once, with a subagent whose whole task is to reply with the single word PONG. Then reply with the single word DONE.",
        (events) => ofType(events, "turn_ended").some((event) => event.startedBy === "owner"),
      );
      const started = ofType(run.events, "task_started");
      const agentCall = ofType(run.events, "call_started").find(
        (event) =>
          event.parentCallId === null && (event.toolName === "Agent" || event.toolName === "Task"),
      );
      const children = run.events.filter(
        (event) =>
          "parentCallId" in event &&
          event.parentCallId !== null &&
          event.parentCallId === agentCall?.callId,
      );
      report(
        agentCall !== undefined && started.some((event) => event.kind === "subagent"),
        "a subagent call gives an Agent call_started and a subagent task_started",
        `Agent call ${agentCall === undefined ? "none" : agentCall.toolName}, task_started kinds ${started.map((event) => event.kind).join(",") || "none"}`,
      );
      const count = (type: string) => children.filter((event) => event.type === type).length;
      const withParent = run.events.filter(
        (event) => "parentCallId" in event && event.parentCallId !== null,
      );
      info(
        "what a subagent's records look like",
        `${children.length} events under the Agent call: message_started ${count("message_started")}, text_delta ${count("text_delta")}, text ${count("text")}, call_started ${count("call_started")}, call_ended ${count("call_ended")}; task_started call id ${started[0]?.callId === agentCall?.callId ? "is the Agent call" : "differs"}, task_progress ${ofType(run.events, "task_progress").length}; events with any parent call id ${withParent.length} (${[...new Set(withParent.map((event) => event.type))].join(",")}); event types in the turn ${[...new Set(run.events.map((event) => event.type))].join(",")}`,
      );
      await fresh.session.close();
    });

    // 10. A multi-select question.
    await section("question", async () => {
      owner.questions.length = 0;
      owner.onQuestion = (request) => {
        const answers: Record<string, string | string[]> = {};
        for (const question of request.questions) {
          const labels = question.options.map((option) => option.label);
          answers[question.text] = question.multiSelect ? labels : (labels[0] ?? "");
        }
        return { answered: true, answers };
      };
      const run = await turn(
        session,
        recorder,
        "Use the AskUserQuestion tool once with one question, header Pick, multiSelect true and the two options Alpha and Beta. Then reply with the options I picked, separated by a comma, and nothing else.",
      );
      const asked = owner.questions[0];
      const multi = asked?.questions.find((question) => question.multiSelect);
      const said = finalTextOf(run.events);
      info(
        "is a multi-select question's answer accepted as a list",
        `${multi === undefined ? "the model asked no multi-select question" : `asked ${multi.options.length} options`}; reply mentions Alpha: ${said.includes("Alpha")}, Beta: ${said.includes("Beta")}; ${ofType(run.events, "call_ended").filter((event) => event.isError).length} errored call(s)`,
      );
      report(
        multi !== undefined && said.includes("Alpha") && said.includes("Beta"),
        "a question reaches the handler and its list answer reaches the model",
        `question requests ${owner.questions.length}, multi-select ${multi !== undefined}, reply holds both options: ${said.includes("Alpha") && said.includes("Beta")}`,
      );
    });

    // 11. Close, then resume the same id right after.
    await section("close and resume", async () => {
      const began = Date.now();
      await session.close();
      const closedIn = Date.now() - began;
      report(
        recorder.ended,
        "close resolves once the stream has ended",
        `closed in ${closedIn} ms`,
      );
      const sawLost = recorder.events.some((event) => event.type === "process_lost");
      report(!sawLost, "a closed session gives no process_lost", `process_lost ${sawLost}`);
      if (sessionId === null) {
        report(false, "a second session resumes the same id", "no session id was seen");
        return;
      }
      const again = await open(directory, sessionId);
      const run = await turn(
        again.session,
        again.recorder,
        "What was the codeword I gave you? Reply with the codeword only.",
      );
      const said = finalTextOf(run.events);
      report(
        said.includes(CODEWORD),
        "a second session resumes the same id and remembers the first prompt",
        `start took ${again.startedIn} ms, the reply holds the codeword: ${said.includes(CODEWORD)}`,
      );
      await again.session.close();
    });

    // 12. A resume Claude Code refuses.
    await section("refused resume", async () => {
      const began = Date.now();
      try {
        await backend.start(options(directory, randomUUID()), owner);
        report(false, "a resume Claude Code refuses is ResumeRefused", "the start succeeded");
      } catch (error) {
        report(
          error instanceof ResumeRefused,
          "a resume Claude Code refuses is ResumeRefused",
          `${error instanceof Error ? error.name : typeof error} after ${Date.now() - began} ms`,
        );
      }
    });

    // 13. A folder whose name is past the plain limit: do we derive the CLI's folder name?
    await section("long folder", async () => {
      const long = await folder(join("l".repeat(100), "m".repeat(100), "n".repeat(100)));
      const key = await projectKey(long);
      const deep = await open(long);
      await turn(deep.session, deep.recorder, "Reply with the single word ok.");
      const id = ofType(deep.recorder.events, "session_started")[0]?.sessionId ?? "";
      const alive = await backend.aliveSessions(long);
      info(
        "does the derived name of a long folder match the CLI's",
        `key of ${key.length} characters (limit ${LONG_PROJECT_KEY}); alive set ${alive === null ? "null: the folder was not found under that name" : `has the session: ${alive.has(id)}`}`,
      );
      report(
        alive?.has(id) === true,
        "a folder named past 200 characters is found under the derived name",
        alive === null ? "cannot tell" : `alive set of ${alive.size}`,
      );
      await deep.session.close();
    });
  } finally {
    await Promise.allSettled(sessions.map((session) => session.close()));
    await removeTranscripts();
    await rm(root, { recursive: true, force: true });
  }
}

try {
  await main();
} catch (error) {
  failures += 1;
  console.log(`FAIL live check: threw ${error instanceof Error ? error.name : typeof error}`);
}
console.log(failures === 0 ? "ALL PASS" : `${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
