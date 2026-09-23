# Architecture

code-with-slack is one Python process. It holds a Slack Socket Mode connection and one Claude
Agent SDK client per bound channel, all on one asyncio event loop.

## Configuration

`code_with_slack.config.load_config` reads `~/.config/code-with-slack/.env` with
`python-dotenv`, without copying anything into the process environment. It refuses a file that
is not a regular file, belongs to another user, or is readable by group or others, and it
refuses tokens of the wrong kind. [setup.md](setup.md) lists the variables.
