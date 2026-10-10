/**
 * The few tools the terminal shows in words of its own, and nothing else.
 *
 * Rendering stays generic: a tool not named here shows as its name, counted when it ran more
 * than once, and its line, with no change anywhere.
 */
import { fill } from "../texts.ts";

// How the terminal folds finished calls of these tools. Captured from the terminal on Claude Code
// 2.1.283 (2026-09-27), where Bash also does the searching (the CLI has no Grep or Glob tool):
// `echo hi` read `Ran 1 shell command`, `ls` `Listed 1 directory`, a grep `Searched for 1
// pattern`. That classification of the command is undocumented, so every Bash call reads here as
// a shell command.
export const WORDS: Readonly<Record<string, readonly [one: string, many: string]>> = {
  Bash: ["Ran {n} shell command", "Ran {n} shell commands"],
  Read: ["Read {n} file", "Read {n} files"],
};

/** How `n` finished calls of `name` read in a folded line. */
export function folded(name: string, n: number): string {
  const words = Object.hasOwn(WORDS, name) ? WORDS[name] : undefined;
  if (words === undefined) {
    return n === 1 ? name : `${name} ×${n}`;
  }
  return fill(n === 1 ? words[0] : words[1], { n });
}
