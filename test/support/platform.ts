/**
 * Folder trust and the daemon's own git read a repository's layout from the filesystem, by
 * rules written for POSIX paths. On Windows, which is not a supported host yet, 54 of those
 * tests failed on their first run (2026-10-10, Node 22 on windows-latest) and none was looked
 * into past the first: a test that builds a git layout is skipped there.
 */
export const GIT_LAYOUT = {
  skip: process.platform === "win32" ? "git layouts are read by POSIX rules" : false,
};
