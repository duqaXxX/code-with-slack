/** Claude Code's `/usage` text read into the 5-hour and weekly limits. */

// `/usage` answers in seconds; past this the probe gives up, so the footer never stops refreshing.
export const USAGE_TIMEOUT = 60_000;
// Wording measured on Claude Code 2.1.280 (2026-09-23). A change hides the field, nothing more.
const SESSION_LINE = /^Current session: (\d+)% used(?: · resets (.+))?$/m;
const WEEK_LINE = /^Current week \(all models\): (\d+)% used(?: · resets (.+))?$/m;
const RESET =
  /^(?<mon>[A-Z][a-z]{2}) (?<day>\d{1,2}) at (?<hour>\d{1,2})(?::(?<min>\d{2}))?(?<ampm>am|pm) \((?<tz>[^)]+)\)$/;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAY_MS = 86_400_000;

export interface Limit {
  readonly percent: number;
  /** Milliseconds since the epoch. */
  readonly resetsAt: number | null;
}

export interface Usage {
  readonly session: Limit | null;
  readonly week: Limit | null;
}

const formatters = new Map<string, Intl.DateTimeFormat | null>();

/** The zone's formatter, null for a zone the runtime does not know. */
function formatterFor(zone: string): Intl.DateTimeFormat | null {
  // `zoneinfo` knows no zone named by an offset; `Intl` reads "+02:00" as one.
  if (/^[+-]/.test(zone)) {
    return null;
  }
  let formatter = formatters.get(zone);
  if (formatter === undefined) {
    try {
      formatter = new Intl.DateTimeFormat("en-US", {
        timeZone: zone,
        hourCycle: "h23",
        year: "numeric",
        month: "numeric",
        day: "numeric",
        hour: "numeric",
        minute: "numeric",
        second: "numeric",
      });
    } catch (error) {
      if (!(error instanceof RangeError)) {
        throw error;
      }
      formatter = null;
    }
    formatters.set(zone, formatter);
  }
  return formatter;
}

function wallClock(at: number, formatter: Intl.DateTimeFormat): Record<string, number> {
  const parts: Record<string, number> = {};
  for (const part of formatter.formatToParts(at)) {
    if (part.type !== "literal") {
      parts[part.type] = Number(part.value);
    }
  }
  return parts;
}

/** The zone's offset from UTC at an instant, in milliseconds. */
function offsetAt(at: number, formatter: Intl.DateTimeFormat): number {
  const w = wallClock(at, formatter);
  const local = Date.UTC(
    w.year as number,
    (w.month as number) - 1,
    w.day as number,
    w.hour,
    w.minute,
    w.second,
  );
  return local - Math.floor(at / 1000) * 1000;
}

/**
 * The instant a wall-clock time has in a zone, as `zoneinfo` reads it with `fold=0`: a time the
 * zone repeats is its first occurrence, and one it skips takes the offset from before the jump.
 */
function instantOf(wall: number, formatter: Intl.DateTimeFormat): number {
  const before = offsetAt(wall - DAY_MS, formatter);
  const after = offsetAt(wall + DAY_MS, formatter);
  const valid = [before, after]
    .map((offset) => wall - offset)
    .filter((instant) => offsetAt(instant, formatter) === wall - instant);
  return valid.length > 0 ? Math.min(...valid) : wall - before;
}

/** The instant in `text` ("Sep 24 at 1:10am (Europe/Berlin)"), null when it cannot be read. */
export function parseReset(text: string, now: number): number | null {
  const found = RESET.exec(text.trim())?.groups;
  if (found === undefined) {
    return null;
  }
  const month = MONTHS.indexOf(found.mon as string);
  const formatter = formatterFor(found.tz as string);
  if (month < 0 || formatter === null) {
    return null;
  }
  const hour = (Number(found.hour) % 12) + (found.ampm === "pm" ? 12 : 0);
  const day = Number(found.day);
  const minute = Number(found.min ?? 0);
  const at = (year: number): number | null => {
    const wall = new Date(Date.UTC(year, month, day, hour, minute));
    // A day or a minute out of range is a date that does not exist, not the next month's.
    if (minute > 59 || wall.getUTCMonth() !== month || wall.getUTCDate() !== day) {
      return null;
    }
    return instantOf(wall.getTime(), formatter);
  };
  // The year is the one `now` has in the reset's own zone.
  const year = wallClock(now, formatter).year as number;
  const instant = at(year);
  if (instant === null || instant >= now - DAY_MS) {
    return instant;
  }
  return at(year + 1);
}

function parseLimit(pattern: RegExp, text: string, now: number): Limit | null {
  const match = pattern.exec(text);
  if (match === null) {
    return null;
  }
  const reset = match[2];
  return { percent: Number(match[1]), resetsAt: reset ? parseReset(reset, now) : null };
}

/** The 5-hour and weekly figures from `/usage`; a field it cannot read is null. */
export function parseUsage(text: string, now: number): Usage {
  return { session: parseLimit(SESSION_LINE, text, now), week: parseLimit(WEEK_LINE, text, now) };
}
