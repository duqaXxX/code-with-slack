# Limits

What you can do with Claude Code in the terminal and cannot do, or do differently, when you
drive it from Slack through code-with-slack. Each limit is listed under whoever sets it, since
that says whether it can change: a limit of the Claude Agent SDK applies to any program that
drives Claude Code, a limit of Slack to any Slack app, and a choice of code-with-slack is this
project's own.

A limit that has an equivalent you would not notice is left out. `!status`, `!help` and
`!resume` stand in for the terminal's `/status`, `/help` and `/resume`, `!stop` interrupts as
Esc does, and a command of Claude Code is typed with `!` in place of `/`. The limits of a
single command (how many
folders `!bind` lists, how large a file `!open` shares) are in [setup.md](setup.md), beside the
command.

## Limits of the Claude Agent SDK

code-with-slack drives Claude Code through the Claude Agent SDK, which runs it without its
interactive terminal. Claude Code keeps some features for the interactive terminal, and answers
a command it does not offer there with `/<name> isn't available in this environment.`
code-with-slack passes every command through as you type it and decides none of this.

| What you cannot do | In the terminal | From Slack |
|---|---|---|
| Go back to an earlier point of the conversation or of the code | `/rewind` | Not offered |
| Plan before any edit | `/plan`, or the mode switch on the keyboard | Not offered |
| Branch the conversation, or ask a side question | `/branch`, `/fork`, `/subtask`, `/btw`, `/background` | Not offered |
| Add or change the working directory of a running session | `/add-dir`, `/cd` | Not offered; a thread works in the folder its channel was bound to |
| Review the changes, export or copy a reply | `/diff`, `/export`, `/copy` | Not offered; ask Claude for the diff, and copy from Slack |
| Open the panels that manage a session | `/permissions`, `/memory`, `/hooks`, `/skills`, `/tasks`, `/sandbox` | Not offered; the settings you made in the terminal apply |
| Let Claude use the apps on your screen | Computer use, a server built into Claude Code | Not available: Claude Code offers it in an interactive session only |
| Dictate a message | `/voice`, from the microphone | Not offered; an audio clip is the closest thing, below |

Measured on 2026-10-09 with Claude Code 2.1.292, the version the pinned SDK bundles. Claude
Code's commands reference lists 119 built-in commands; a session started through the SDK is
offered 50 of them. Twenty-two of the others were each sent as code-with-slack sends a command:
the nineteen of the table, `/status`, `/help` and `/chrome`. Each got the answer quoted above,
at no cost in tokens.
Computer use was absent from the session's servers although it was switched on for the folder.

For two rows the SDK has a way of its own, which code-with-slack does not use. A program can
set a session's permission mode to `plan`, and it can rewind the files of a session to an
earlier message. Offering either from Slack would be a feature of this project to design, and
neither exists today.

## Limits of Slack

| What differs | In the terminal | From Slack |
|---|---|---|
| Speaking a message | Dictation writes your words as you speak | You send an audio clip and choose **Generate transcript** on it: Slack writes no transcript by itself |
| The language of what you said | The `language` setting decides | Slack picks the language it hears, clip by clip, and nothing lets an app choose it |

Of three clips spoken in Italian in a workspace set to English (Slack free plan, iOS app,
2026-10-09), Slack first wrote one in Italian and two as English words that were not what was
said. Later reads of the same clips gave Italian, and what changed in between is not known. The
transcript is what reaches Claude, so read it under the clip before you rely on the answer.

## Done on the machine, not from Slack

These need the terminal on the machine that runs the sessions, once or now and then. Away from
it they cannot be done.

| What | Why |
|---|---|
| Logging Claude Code in or out | `claude` and `/login` on the machine. `!login` and `!logout` answer with that pointer |
| Signing in to an MCP server that asks for it | Claude Code runs that flow in the terminal: `claude mcp login <name>`, or `/mcp` |
| Trusting a folder you have not used before | Claude Code shows its trust dialog in the terminal only, and a session starts only in a trusted folder |

## Choices of code-with-slack

| What | In the terminal | With code-with-slack |
|---|---|---|
| Who can use it | Whoever sits at the machine | One Slack user, in private channels that hold that user and the bot, in a workspace with no other member |
| Where it runs | macOS, Linux and Windows | macOS, as a LaunchAgent in your login session |
| Permission modes | Every mode Claude Code has | Bypass on or off for a thread; any other mode is the one your Claude Code settings start in |

## Not checked

- The 47 commands Claude Code does not offer to an SDK session and that are not named above were
  not sent one by one. Most are outside daily use (themes, key bindings, installers).
- A command that is offered can still open a dialog in the terminal (`/config`, `/mcp`,
  `/agents`): what each does from Slack was not tried.
- Linux and Windows hosts were never run.
