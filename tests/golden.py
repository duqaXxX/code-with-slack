"""Golden files for the TypeScript port: what the Python code does at two seams, for every
recorded SDK stream. Run from the repo root with `uv run python -m tests.golden`; it rewrites
everything under `test/golden/` (the TypeScript test tree), and `tests/test_golden.py` fails when
the committed files differ from what this produces.

Seam 1 (`reply/<name>.json`): the calls `TurnRenderer` makes on its `Sink`, in order.
Seam 2 (`slack/<name>.json`): the Slack calls a real `ReplySink` makes for those sink calls.
Texts (`texts.json`): every constant of `awaydesk.texts`, and the call of every public function of
it on fixed arguments.

The files must be byte-for-byte reproducible: no wall clock, no sets, no `default=str`."""

import argparse
import asyncio
import dataclasses
import inspect
import json
import shutil
import string
import subprocess
from importlib.metadata import version
from pathlib import Path
from typing import Any

from slack_sdk.web.async_slack_response import AsyncSlackResponse

from awaydesk import texts
from awaydesk.render.previews import Preview
from awaydesk.render.renderer import TaskUpdate, TurnRenderer
from awaydesk.render.sinks import ReplySink, UpdateLimiter
from tests.fakes import (
    BOT,
    CHANNEL,
    FIXTURES,
    OWNER,
    TEAM,
    THREAD,
    FakeClock,
    FakeSlack,
    sdk_messages,
    split_turns,
)

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "test" / "golden"
FOOTER = "footer"

# Nothing in the replay waits on a timer, and the Slack write budget never delays one.
DISCIPLINE = (
    "Each sink call of the seam-1 'whole' list is replayed in order on a ReplySink over FakeSlack. "
    "After a text or task call, ReplySink.settle() flushes the reply at once, standing in for the "
    "debounced write (one flush per sink call, so the debounce never merges two calls); finish "
    "and close_out flush by themselves. The fake clock never advances, so the 280-second stream "
    "deadline never fires, and the UpdateLimiter has a budget no recording can spend."
)


def recordings() -> list[str]:
    return sorted(p.stem for p in (FIXTURES / "sdk").glob("*.jsonl"))


def dump(value: Any) -> str:
    """The file format: sorted keys, UTF-8 as written, a trailing newline. A value JSON cannot
    hold raises instead of being turned into text."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


# Seam 1: the renderer's calls on its sink.


class RecordingSink:
    """Every call the renderer makes on its `Sink`, as the plain objects the golden files hold."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def text(self, markdown: str, *, notice: bool = False, ending: bool = False) -> None:
        self.calls.append(
            {"call": "text", "markdown": markdown, "notice": notice, "ending": ending}
        )

    async def task(self, update: TaskUpdate) -> None:
        self.calls.append({"call": "task", "update": dataclasses.asdict(update)})

    async def finish(self, closing: list[TaskUpdate]) -> None:
        self.calls.append({"call": "finish", "closing": [dataclasses.asdict(u) for u in closing]})

    async def close_out(self, footer: str | None) -> bool:
        self.calls.append({"call": "close_out", "footer": footer})
        return True

    async def wait_landed(self) -> bool:
        self.calls.append({"call": "wait_landed"})
        return True

    async def settle(self) -> bool:
        self.calls.append({"call": "settle"})
        return True


async def replay_reply(messages: list[Any]) -> dict[str, Any]:
    """One renderer fed `messages`, then closed as a finished turn is. An exception is recorded
    by its type, with the calls made before it."""
    sink = RecordingSink()
    renderer = TurnRenderer(sink)
    try:
        for message in messages:
            await renderer.feed(message)
        await renderer.close(FOOTER)
        await renderer.close_out()
    except Exception as exc:
        return {"raises": type(exc).__name__, "calls": sink.calls}
    return {
        "calls": sink.calls,
        "result_set": renderer.result is not None,
        "running_tasks": renderer.running_tasks,
    }


async def reply_golden(name: str) -> dict[str, Any]:
    messages = sdk_messages(name)
    return {
        "recording": name,
        "whole": await replay_reply(messages),
        "turns": [await replay_reply(turn) for turn in split_turns(messages)],
    }


# Seam 2: the Slack calls of a ReplySink.


class RecordingSlack(FakeSlack):
    """FakeSlack that also keeps what each call answered, with the sink call that caused it."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: list[dict[str, Any]] = []

    async def api_call(  # type: ignore[override]
        self, api_method: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        response = await super().api_call(api_method, **kwargs)
        assert isinstance(response.data, dict)
        self.answers.append(response.data)
        return response


def task_update(fields: dict[str, Any]) -> TaskUpdate:
    preview = fields["preview"]
    return TaskUpdate(**{**fields, "preview": Preview(**preview) if preview else None})


async def replay_slack(calls: list[dict[str, Any]]) -> dict[str, Any]:
    slack = RecordingSlack()
    sink = ReplySink(
        slack,
        channel=CHANNEL,
        thread_ts=THREAD,
        team_id=TEAM,
        user_id=OWNER,
        bot_user_id=BOT,
        limiter=UpdateLimiter(limit=10**9, burst=10**9),
        clock=FakeClock(),
    )
    caused: list[int] = []  # the index of the sink call behind each Slack call
    for index, call in enumerate(calls):
        match call["call"]:
            case "text":
                await sink.text(call["markdown"], notice=call["notice"], ending=call["ending"])
                await sink.settle()
            case "task":
                await sink.task(task_update(call["update"]))
                await sink.settle()
            case "finish":
                await sink.finish([task_update(u) for u in call["closing"]])
            case "close_out":
                await sink.close_out(call["footer"])
            case "wait_landed":
                await sink.wait_landed()
            case "settle":
                await sink.settle()
            case other:
                raise ValueError(f"unknown sink call {other!r}")
        caused += [index] * (len(slack.calls) - len(caused))
    await asyncio.sleep(0)  # the debounce tasks settle() cancelled finish dying
    return {
        "slack_calls": [
            {"after_sink_call": cause, "method": method, "args": args, "answer": answer}
            for cause, (method, args), answer in zip(
                caused, slack.calls, slack.answers, strict=True
            )
        ],
        "final": {
            "message_blocks": slack.message_blocks(),
            "message_cards": slack.message_cards(),
            "message_texts": slack.message_texts(),
            "pushes": slack.pushes(),
        },
    }


async def slack_golden(name: str, reply: dict[str, Any]) -> dict[str, Any]:
    whole = reply["whole"]
    body: dict[str, Any]
    if "raises" in whole:
        body = {"input_raises": whole["raises"]}
    else:
        body = await replay_slack(whole["calls"])
    return {"recording": name, "discipline": DISCIPLINE, **body}


# The texts.

# The arguments each public function of `awaydesk.texts` is recorded with, as (args, kwargs). The
# module holds constants only today: a function added to it fails `texts_golden` until it is
# listed here, so the TypeScript port cannot miss it.
TEXT_CALLS: dict[str, list[tuple[list[Any], dict[str, Any]]]] = {}


def template_fields(template: str) -> list[str]:
    return [field for _, field, _, _ in string.Formatter().parse(template) if field]


def texts_golden() -> dict[str, Any]:
    """Every module-level constant of `awaydesk.texts` by name (a tuple of strings as a list), the
    output of `str.format` for each template on one value per field, and the recorded calls of
    its public functions."""
    constants: dict[str, str] = {}
    sequences: dict[str, list[str]] = {}
    for name, value in vars(texts).items():
        if name.startswith("_"):
            continue
        if isinstance(value, str):
            constants[name] = value
        elif isinstance(value, tuple) and all(isinstance(item, str) for item in value):
            sequences[name] = list(value)
    fills = {}
    for name, template in constants.items():
        fields = template_fields(template)
        if fields:
            values = {field: f"<{field}>" for field in fields}
            fills[name] = {"values": values, "output": template.format(**values)}
    functions = sorted(
        name
        for name, value in vars(texts).items()
        if inspect.isfunction(value) and value.__module__ == texts.__name__ and name[0] != "_"
    )
    if unlisted := [name for name in functions if name not in TEXT_CALLS]:
        raise ValueError(f"texts functions without recorded calls: {unlisted}")
    calls = [
        {
            "function": name,
            "args": args,
            "kwargs": kwargs,
            "output": getattr(texts, name)(*args, **kwargs),
        }
        for name in functions
        for args, kwargs in TEXT_CALLS[name]
    ]
    return {
        "constants": constants,
        "sequences": sequences,
        "fills": fills,
        "functions": functions,
        "calls": calls,
    }


# The files.


def python_commit() -> str:
    """The last commit that touched the Python source: stable across commits that do not."""
    out = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", "src/awaydesk"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


async def build() -> dict[str, str]:
    """Every golden file, by its path under `test/golden/`."""
    names = recordings()
    files = {
        "manifest.json": dump(
            {
                "claude_agent_sdk": version("claude-agent-sdk"),
                "python_commit": python_commit(),
                "recordings": names,
            }
        )
    }
    for name in names:
        reply = await reply_golden(name)
        files[f"reply/{name}.json"] = dump(reply)
        files[f"slack/{name}.json"] = dump(await slack_golden(name, reply))
    files["texts.json"] = dump(texts_golden())
    return files


def generate(out: Path = GOLDEN) -> None:
    """Rewrite the golden tree under `out`; files of a recording that is gone are removed."""
    files = asyncio.run(build())
    for seam in ("reply", "slack"):
        shutil.rmtree(out / seam, ignore_errors=True)
    for relative, text in files.items():
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Regenerate the golden files for the TS port.")
    parser.add_argument(
        "--out", type=Path, default=GOLDEN, help="directory to write (default: test/golden)"
    )
    generate(parser.parse_args().out)


if __name__ == "__main__":
    main()
