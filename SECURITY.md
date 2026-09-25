# Security

## Reporting a vulnerability

Report it privately through GitHub: the repository's **Security** tab, **Report a
vulnerability**. Do not open a public issue.

## Scope

code-with-slack runs Claude Code on the owner's machine on behalf of one Slack user. In scope:

- any path by which someone other than the configured owner, or an event from another workspace,
  reaches a Claude Code session, an approval, or a command;
- output reaching a channel that is public, shared, or has a member other than the owner and the
  bot;
- secrets (`.env` tokens) written anywhere other than `~/.config/code-with-slack/.env`, or message
  content written to a log;
- an approval request that shows the owner something other than what Approve lets run;
- a session started in a folder the owner has not trusted in Claude Code.

Out of scope: what Claude Code does once the owner approves it, and whoever controls the owner's
Slack account, who controls the machine by design. [docs/setup.md](docs/setup.md) lists the
settings that protect against that.
