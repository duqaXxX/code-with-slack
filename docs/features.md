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
| `!bind` and the folder list, only folders Claude Code trusts | `test_folders`, `test_trust`, `test_slack_app`, `test_state` | none | Bind a folder you trusted in the terminal the same day |
| A prompt gets one reply, rewritten as it grows, split past Slack's limits | `test_sessions`, `test_sinks` | P1, P3 | How a long reply reads on desktop and on mobile |
| Tool lines fold in the terminal's words (`Ran 2 shell commands · Read 1 file`) | `test_sinks`, `test_previews` | P10 | none |
| Edit and Write previews: sentence, diff or new file's lines | `test_previews`, `test_sinks` | P13 | A diff opens and closes on desktop and mobile; it colours on desktop, the squares on mobile |
| Notifications: one per complete reply, approval, question or error, none while Claude writes | `test_sinks`, `test_sessions` | none | With the channel on Just mentions, away from Slack on the desktop: a reply rings once, `!stop` does not |
| Approvals: Approve and Deny buttons for a tool call | `test_approvals`, `test_sessions`, `test_slack_app` | P11 | none |
| Questions: the Answer form, and the answered record kept in the channel | `test_approvals`, `test_slack_app` | none | Answer a real question; the record stays with each answer |
| `!bypass`, kept in `state.json` across restarts | `test_sessions`, `test_state`, `test_slack_app` | P9 | none |
| `!stop` ends the running turn and the channel's background tasks | `test_sessions`, `test_slack_app` | P8, P12 | none |
| `!status` and the footer (branch and changes of the folder the session works in, model, context, tokens, usage limits and their resets) | `test_footer`, `test_sessions`, `test_slack_app` | P2, P14 | none |
| `!resume`: the session list and resuming one | `test_resume`, `test_sessions`, `test_slack_app` | P6, P7 | none |
| Effort and model kept across a resume | `test_sessions` | P15, P16 | none |
| `!help`, `!guide`, and `!name` sent to Claude Code as `/name` | `test_commands`, `test_slack_app` | none | none |
| Attached images and files | `test_attachments`, `test_slack_app` | P4, P5 | none |
| Background commands and subagents: their lines, the running count, Claude Code's report | `test_renderer`, `test_sinks`, `test_sessions` | none | Run a subagent and a background command; each line closes when it ends |
| A restart lets running turns finish, and says which background tasks hold it | `test_sessions` | none | `launchctl kill TERM` with a turn running; it finishes, then the daemon reconnects |
| A logged-out Claude Code gets the login instructions | `test_renderer` | none | Log out in the terminal, send a message |
| Configuration: `.env` private, every missing variable named | `test_config` | none | none |
| One instance at a time | `test_lock`, `test_main` | none | none |
| The LaunchAgent and the Slack app installed from `docs/setup.md` | `test_main` (the manifest) | none | Follow `docs/setup.md` on a new machine |

## Notifications

Notifications work by mention. The owner sets each channel to Slack's **Just mentions**
([setup](setup.md#create-a-channel-per-project)), and the bot writes `@channel` only in the
messages that need the owner:

- A complete reply rings once: the mention ends the footer line, in a closing message posted
  when the turn ends.
- An approval request and a question (AskUserQuestion) ring: the mention ends the request.
- A reply that ends in an error rings, once per failure.
- Nothing else rings: not `Claude is writing…`, not a rewrite, not the continuation of a reply
  longer than one message, not a reply ended by `!stop`, a restart or a rebind, and not a reply
  Claude Code starts on its own to report a background task (the prompt that started the task
  already rang).

The notification text reads `Reply to: ` and the start of the owner's message. The events follow
the terminal, which notifies when Claude finishes or waits for a permission.

Why it is built this way. Slack lets a user choose notifications per channel, never per
message, and its reference says nothing about which bot messages notify. Eleven probe messages
sent on 2026-09-27 to an iPhone (Slack iOS app, channel on Just mentions) showed:

- only a new message rings: a `chat.update` never does, even one that adds `@channel`, so a reply
  that is rewritten as it grows cannot ring when it ends, and the end is posted as a message of
  its own;
- with `@channel` in the notification text alone the phone stayed silent in most probes, so the
  mention sits in the message itself, where the channel shows it;
- `@here` reaches only the members Slack counts as active, so it misses an owner who is away.

What is not covered: a second ringing message from the same channel within about a minute of a
first one was silent in most probes, which Slack does not document; Android and the desktop
client were not tested.
