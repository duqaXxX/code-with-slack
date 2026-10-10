/**
 * The loggers the Slack libraries are given, so that none of their text reaches the log.
 *
 * Read in `@slack/web-api` 8.2.0, `@slack/socket-mode` 3.1.0 and `@slack/bolt` 5.1.0 (`dist/`):
 * with no `logger` option each builds a console logger of its own, which writes to standard
 * output and standard error, outside `src/log.ts`. Their lines carry text the daemon withholds
 * elsewhere (`describeRefusal` keeps Slack's sentence out because it can quote a value of the
 * message): every `[ERROR]` and `[WARN]` entry of a refusal's `response_metadata.messages`, the
 * failure message of a request, `${error}` of an error Bolt's receiver reports. At the debug
 * level they also write each request body and each result, which hold the message text and, for
 * `apps.connections.open`, the WebSocket URL with its ticket; Bolt's `processEvent` does the
 * same with the whole event, in developer mode only.
 *
 * A custom `logger` replaces the library's own and its `logLevel` is then ignored, so the
 * debug text is never forwarded here either. A failure the libraries report this way also
 * reaches the code that called them, as a typed error the daemon logs by its name
 * (`describe` in `reply/errors.ts`), or, for a Socket Mode connection that cannot come back, as
 * the unhandled rejection that stops the daemon (`main.ts`).
 */

import type { Logger as LibraryLogger } from "@slack/web-api";
import { LogLevel } from "@slack/web-api";
import { getLogger } from "../../log.ts";

export const logger = getLogger("awaydesk.chat.slack.libraries");

function quiet(write: (message: string) => void): LibraryLogger {
  return {
    debug: () => {},
    info: () => {},
    warn: () => write("warning"),
    error: () => write("error"),
    setLevel: () => {},
    // Warn: a library that asks builds no debug string (Socket Mode's `ack` JSON-encodes its
    // answer only at the debug level).
    getLevel: () => LogLevel.WARN,
    setName: () => {},
  };
}

/** Writes nothing, at any level: the web clients, whose failures come back as typed errors. */
export function silentLogger(): LibraryLogger {
  return quiet(() => {});
}

/**
 * Writes one line per warning or error the library reports, naming the library and the level and
 * nothing the library said: Bolt's app and its Socket Mode receiver, whose lines name no method
 * and no id the daemon could add to.
 */
export function levelLogger(library: string): LibraryLogger {
  return quiet((level) => {
    if (level === "error") logger.error(`${library} reported an error`);
    else logger.warning(`${library} reported a warning`);
  });
}
