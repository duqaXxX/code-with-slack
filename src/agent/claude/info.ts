/**
 * What Claude Code says of itself, into the seam's types: its models, its commands and the
 * permission mode a session starts in (the answer to the initialize request), the context usage,
 * and a listed session.
 *
 * The first two are read as `unknown`: the initialize answer carries `current_permission_mode`,
 * which `SDKControlInitializeResponse` does not declare (measured on Claude Code 2.1.286,
 * `tests/fixtures/sdk/server-info.json`).
 */
import type { SDKSessionInfo } from "@anthropic-ai/claude-agent-sdk";
import type { AgentInfo, CommandInfo, ContextUsage, ListedSession, ModelInfo } from "../seam.ts";
import { isRecord, number, records, string, type WireRecord, words } from "./wire.ts";

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry) => typeof entry === "string") : [];
}

function modelOf(entry: WireRecord): ModelInfo | null {
  // The setup keys every option on `value`: an entry without one is left out here, so a CLI
  // that lists one cannot stop every first prompt.
  const value = words(entry.value);
  if (value === null) return null;
  const supportsEffort = entry.supportsEffort === true;
  return {
    value,
    displayName: words(entry.displayName) ?? value,
    description: words(entry.description),
    supportsEffort,
    supportedEffortLevels: supportsEffort ? strings(entry.supportedEffortLevels) : [],
  };
}

function commandOf(entry: WireRecord): CommandInfo | null {
  const name = words(entry.name);
  if (name === null) return null;
  return {
    name,
    description: string(entry.description) ?? "",
    argumentHint: string(entry.argumentHint) ?? "",
    aliases: strings(entry.aliases),
  };
}

function known<T>(entries: readonly (T | null)[]): T[] {
  return entries.filter((entry): entry is T => entry !== null);
}

/** The answer to the initialize request as the models, commands and mode the core reads. */
export function agentInfo(info: unknown): AgentInfo {
  const answer = isRecord(info) ? info : {};
  return {
    models: known(records(answer.models).map(modelOf)),
    commands: known(records(answer.commands).map(commandOf)),
    permissionMode: words(answer.current_permission_mode),
  };
}

/** The context usage as the footer reads it: the model and the share of its window in use. */
export function contextUsage(usage: unknown): ContextUsage {
  const answer = isRecord(usage) ? usage : {};
  return { model: string(answer.model), percentage: number(answer.percentage) };
}

/** A session the SDK lists, as a row of `!resume`. */
export function listedSession(session: SDKSessionInfo): ListedSession {
  return {
    id: session.sessionId,
    title: session.summary,
    customTitle: session.customTitle ?? null,
    // Empty outside a repository's branch: the row then shows none.
    branch: session.gitBranch || null,
    size: session.fileSize ?? null,
    lastModified: session.lastModified,
  };
}
