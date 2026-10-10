/** Paths and strings the way Python's `Path` and `sorted` treated them. */
import { isAbsolute, relative, sep } from "node:path";

/** `Path.is_relative_to` and `relative_to`: `path` from `root` when it lies under it, else null. */
export function relativeTo(root: string, path: string): string | null {
  const found = relative(root, path);
  if (found === ".." || found.startsWith(`..${sep}`) || isAbsolute(found)) return null;
  return found;
}

/** Python's `sorted` on strings: by code point, where `Array.prototype.sort` goes by UTF-16 unit. */
export function compareCodePoints(a: string, b: string): number {
  const left = Array.from(a);
  const right = Array.from(b);
  for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
    const x = left[i] as string;
    const y = right[i] as string;
    if (x !== y) return (x.codePointAt(0) as number) - (y.codePointAt(0) as number);
  }
  return left.length - right.length;
}
