/**
 * The daemon's log: lines on standard error, which the service writes to the log file. A line
 * carries ids, counts and error names, never the text of a prompt or of a reply.
 */

export interface Logger {
  debug(message: string): void;
  info(message: string): void;
  warning(message: string): void;
  error(message: string): void;
}

export type Level = "DEBUG" | "INFO" | "WARNING" | "ERROR";

const ORDER: readonly Level[] = ["DEBUG", "INFO", "WARNING", "ERROR"];

let threshold: Level = "INFO";
let write: (line: string) => void = (line) => {
  process.stderr.write(`${line}\n`);
};

/** The lowest level that is written; `INFO` until set. */
export function setLevel(level: Level): void {
  threshold = level;
}

/** Where the lines go, for a test that reads them; returns the writer it replaced. */
export function setWriter(writer: (line: string) => void): (line: string) => void {
  const previous = write;
  write = writer;
  return previous;
}

function two(value: number): string {
  return String(value).padStart(2, "0");
}

/** `2026-10-10 09:05:03,042`, local time. */
export function timestamp(at: Date): string {
  const day = `${at.getFullYear()}-${two(at.getMonth() + 1)}-${two(at.getDate())}`;
  const time = `${two(at.getHours())}:${two(at.getMinutes())}:${two(at.getSeconds())}`;
  return `${day} ${time},${String(at.getMilliseconds()).padStart(3, "0")}`;
}

/** A logger whose lines read `<time> <LEVEL> <name>: <message>`. */
export function getLogger(name: string): Logger {
  const at = (level: Level) => (message: string) => {
    if (ORDER.indexOf(level) < ORDER.indexOf(threshold)) return;
    write(`${timestamp(new Date())} ${level} ${name}: ${message}`);
  };
  return { debug: at("DEBUG"), info: at("INFO"), warning: at("WARNING"), error: at("ERROR") };
}
