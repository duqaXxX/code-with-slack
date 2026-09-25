#!/usr/bin/env bash
#
# Opens the SDK release watch's issue, or brings the open one up to date.
#
# The issue is the state: there is no counter to keep, so a missed run costs nothing and a run that
# happens twice writes the same thing. The body is rewritten every time, which notifies nobody; a
# comment comes only when sdk_release_report.py writes one.
#
#   GH_TOKEN=... REPO=owner/name PINNED=0.2.158 LATEST=0.2.160 LATEST_CLI=2.1.285 \
#   FIXTURE_CLI=2.1.280 OUTCOME=pass bash .github/scripts/watch-sdk-release.sh
#
# DRY_RUN=1 reads the repository and prints what it would write, touching nothing.
set -euo pipefail

REPO="${REPO:?REPO is required}"
LABEL="sdk release"

number="$(gh issue list --repo "$REPO" --label "$LABEL" --state open --limit 1 \
  --json number --jq '.[0].number // empty')"

# The version the open issue last named in a comment, so a release is announced once.
last_commented=""
if [ -n "$number" ]; then
  last_commented="$(gh issue view "$number" --repo "$REPO" --json comments \
    --jq '[.comments[].body | capture("claude-agent-sdk (?<v>[0-9]+(\\.[0-9]+)+)").v] | last // empty')"
fi

report="$(LAST_COMMENTED="$last_commented" uv run --quiet python .github/scripts/sdk_release_report.py)"
if [ "$report" = "null" ]; then
  echo "the latest release is the pinned one; nothing to report"
  exit 0
fi

title="$(printf '%s' "$report" | jq -r .title)"
comment="$(printf '%s' "$report" | jq -r '.comment // empty')"
body_file="$(mktemp)"
printf '%s' "$report" | jq -r .body > "$body_file"

# A closed issue with this title means the release was checked: with no open issue to update,
# opening one would ask for the same check again.
if [ -z "$number" ]; then
  checked_in="$(TITLE="$title" gh issue list --repo "$REPO" --label "$LABEL" --state closed \
    --limit 100 --json number,title --jq '[.[] | select(.title == env.TITLE)][0].number // empty')"
  if [ -n "$checked_in" ]; then
    echo "$title: checked in #$checked_in; nothing to open"
    exit 0
  fi
fi

if [ -n "${DRY_RUN:-}" ]; then
  echo "would ${number:+update issue #$number}${number:-open an issue}: $title"
  echo "--- body"; cat "$body_file"
  echo "--- comment: ${comment:-none}"
  exit 0
fi

gh label create "$LABEL" --repo "$REPO" --color 5319e7 \
  --description "A claude-agent-sdk release code-with-slack has not been checked against yet" \
  >/dev/null 2>&1 || true

if [ -z "$number" ]; then
  number="$(gh issue create --repo "$REPO" --label "$LABEL" --title "$title" \
    --body-file "$body_file" | grep -oE '[0-9]+$')"
  echo "opened issue #$number"
else
  gh issue edit "$number" --repo "$REPO" --title "$title" --body-file "$body_file" >/dev/null
  echo "updated issue #$number"
fi

if [ -n "$comment" ]; then
  gh issue comment "$number" --repo "$REPO" --body "$comment"
  echo "commented: $comment"
fi
