# Features and how each is checked

Every feature the owner can see, and what verifies it: the test suite on every change, the
release probe (`uv run python -m probe`, described in `CONTRIBUTING.md`) on every new
`claude-agent-sdk`, and what nothing checks automatically. A new feature adds its row in the same
pull request. `tests/test_features.py` fails when a probe claim in `probe/claims.py` is missing
from the Probe column, or the column names a claim that does not exist. The probe prints the By
hand column at the end of every run, as the checklist of what it cannot see.

The suite replays recorded SDK streams and Slack payloads (`tests/fixtures/`), so it cannot see a
change in a new Claude Code CLI: the probe runs the real one. Neither drives the Slack clients, so
how a reply looks on desktop and on mobile is checked by hand.

| Feature | Test suite | Probe | By hand |
|---|---|---|---|
| Owner only: a message, button or form from anyone else does nothing | `test_guards`, `test_slack_app` | none | none |
| Channel guard: a private channel holding the owner and the bot only | `test_guards`, `test_slack_app` | none | none |
| One session per thread: a top-level message opens a new thread with its own session; a reply inside a thread continues that session | `test_sessions`, `test_state`, `test_slack_app` | none | Send two top-level messages in the same channel: each opens its own thread and its own session |
| `!bind` and the folder list, only folders Claude Code trusts; refused inside a session's thread | `test_folders`, `test_trust`, `test_slack_app`, `test_state` | none | Bind a folder you trusted in the terminal the same day |
| A prompt gets one reply, rewritten as it grows, split past Slack's limits | `test_sessions`, `test_sinks` | P1, P3 | How a long reply reads on desktop and on mobile |
| Tool lines fold in the terminal's words (`Ran 2 shell commands · Read 1 file`) | `test_sinks`, `test_previews` | P10 | none |
| Edit and Write previews: sentence, diff or new file's lines | `test_previews`, `test_sinks` | P13 | A diff opens and closes on desktop and mobile; it colours on desktop, the squares on mobile |
| Notifications: one per complete reply, approval, question or error, none while Claude writes | `test_sinks`, `test_sessions` | none | In a thread you started, away from Slack: a reply rings once, `!stop` does not |
| Approvals: Approve and Deny buttons for a tool call, each resolved only in its own thread | `test_approvals`, `test_sessions`, `test_slack_app` | P11 | none |
| Questions: the Answer form, and the answered record kept in the thread | `test_approvals`, `test_slack_app` | none | Answer a real question; the record stays with each answer |
| `!bypass`, kept per thread in `state.json` across restarts; refused at the top level | `test_sessions`, `test_state`, `test_slack_app` | P9 | none |
| `!stop`: at the top level, every session of the channel and its background tasks; inside a thread, that session alone | `test_sessions`, `test_slack_app` | P8, P12 | none |
| `!status`: at the top level, the channel's folder and every live session, each linked to its thread; inside a thread, that session's directory, mode and the footer's values (branch and changes of the folder the session works in, model, context, tokens, usage limits and their resets) | `test_footer`, `test_sessions`, `test_slack_app` | P2, P14 | none |
| `!resume`: the session list and resuming one; a session already open in another thread of any channel is listed with no button and refused if named or clicked | `test_resume`, `test_sessions`, `test_slack_app` | P6, P7 | none |
| Idle close and resume: a thread's Claude Code process closes after an hour with nothing to do and resumes on the next message; top-level `!resume` moves a folder's session into a new thread of its own | `test_sessions`, `test_resume`, `test_slack_app`, `test_state` | none | none |
| Effort kept across a resume: `/effort` set in a thread survives an idle close or a restart | `test_sessions` | P15, P16 | none |
| `!help` at the top level lists the daemon's words and points to a thread for Claude Code's own; inside a thread it lists both; `!guide` and `!name` sent to Claude Code as `/name` work the same either way | `test_commands`, `test_slack_app` | none | none |
| Attached images and files | `test_attachments`, `test_slack_app` | P4, P5 | none |
| Background commands and subagents: their lines, the running count, Claude Code's report | `test_renderer`, `test_sinks`, `test_sessions` | none | Run a subagent and a background command; each line closes when it ends |
| A restart lets running turns finish, prunes threads whose session is gone, and says which background tasks hold it | `test_sessions`, `test_state`, `test_main` | none | `launchctl kill TERM` with a turn running; it finishes, then the daemon reconnects |
| A logged-out Claude Code gets the login instructions | `test_renderer` | none | Log out in the terminal, send a message |
| Configuration: `.env` private, every missing variable named | `test_config` | none | none |
| One instance at a time | `test_lock`, `test_main` | none | none |
| The LaunchAgent and the Slack app installed from `docs/setup.md` | `test_main` (the manifest) | none | Follow `docs/setup.md` on a new machine |

## Notifications

Every reply, approval request and question is a message inside the session's own thread; the
answer to `!bind`, `!status` and `!resume`'s own list, and the upgrade notice, are not: they are
top-level messages of their own.

**Inside a thread**, Slack notifies the owner on a new message in a thread it started, mention or
none. Measured 2026-09-28 (Slack free plan, Slack iOS app, slack-sdk 3.44.1):

- a new bot message in a thread the owner started rang, with or without a mention;
- the same message in a thread the owner did not start stayed silent;
- a `chat.update` rewrite never rang;
- a message posted about one second after the owner's own was sometimes not pushed; at 15
  seconds it always was;
- `@channel` does not notify inside a thread (Slack help center, read 2026-09-28).

So, inside a thread:

- A complete reply rings once, in its closing message. It posts once the turn has ended and none
  of its tasks still runs or still waits on a turn Claude Code starts to report it (D1): a reply
  that starts a background command or an agent still rings for the question it answered, just
  once that task, and its report if one comes, are both done.
- An approval request and a question (`AskUserQuestion`) ring.
- A reply that ends in an error rings once, with a new closing message, on the first owner reply
  a Claude Code process that exits leaves open.
- Nothing else rings: not `Claude is writing…`, not a rewrite (a turn Claude Code starts on its
  own to report a background task edits the reply that started it, never a message of its own),
  not the continuation of a reply longer than one message, and not a reply ended by `!stop`, a
  restart, an idle close or a lost session: these close at once instead of waiting further, with
  no message of their own either, since any new message would still ring whatever it said. The
  footer, if the reply is still the thread's latest, joins the body's own last message with an
  edit instead, which never rings.

The notification text reads `Reply to: ` and the start of the owner's message.

**At the top level**, a message rings only under the channel's own notification setting; with
**Just mentions** ([setup](setup.md#create-a-channel-per-project)) it needs a `@channel` mention,
which the daemon writes nowhere. Slack lets a user choose notifications per channel, never per
message, and its reference says nothing about which bot messages notify. Eleven probe messages
sent on 2026-09-27 to an iPhone (Slack iOS app, channel on Just mentions) showed: only a new
message rings, a `chat.update` never does; a mention in the text alone mostly does not; `@here`
reaches only the members Slack counts as active, so it misses an owner who is away.

What is not covered: a second ringing message from the same channel within about a minute of a
first one was silent in most 2026-09-27 probes, which Slack does not document; Android and the
desktop client were not tested.
