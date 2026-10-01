"""A run of tool calls as two cards: what ended, folded into counts, and under it the call that
runs now. A stream can only append and a card is what Slack updates in place, so the fold the
channel model drew as two lines of text is drawn here as two cards whose titles change.

A run is the calls between two pieces of text. Its first card shows the first call; once a call
has ended and another is shown, that card turns into the counts (`Ran 2 shell commands · Read 1
file · ✗ Ran 1 shell command`) and a second card takes the call shown. The call shown is the last
one started that still runs, else the last one shown: it joins the counts when another takes its
place, as the terminal folds a call once the next one follows.

A call with a view of its own never folds: an Edit or a Write that ended well (its preview), a
subagent or a background command (its task card), a stopped call. It keeps the card that shows
it, or gets one, and the run takes no new call after it.

Neither card carries `details` or `output`: Slack appends both to what a card already holds
(measured 2026-10-01), so a card that is reused keeps its text in its title.
"""

from dataclasses import dataclass, field, replace

from code_with_slack.render.previews import folded
from code_with_slack.render.renderer import STOPPED, TaskStatus, TaskUpdate

ENDED = ("complete", "error")
SEPARATOR = " · "
OK, FAILED = "✓", "✗"


def folds(update: TaskUpdate) -> bool:
    """Whether a call is drawn in its run's two cards rather than on a card of its own."""
    return (
        bool(update.name)
        and not update.task
        and update.shown_preview is None
        and update.output != STOPPED
    )


def counts(calls: list[TaskUpdate], *, check: bool) -> str:
    """Ended calls as counts in the terminal's words, the failed ones after `✗`; `check` puts
    `✓` before the ones that ended well (a card draws that icon itself)."""
    names: dict[str, dict[str, int]] = {"complete": {}, "error": {}}
    for call in calls:
        group = names[call.status]
        group[call.name] = group.get(call.name, 0) + 1
    parts = [
        icon + SEPARATOR.join(folded(name, n) for name, n in group.items())
        for icon, group in (
            (f"{OK} " if check else "", names["complete"]),
            (f"{FAILED} ", names["error"]),
        )
        if group
    ]
    return SEPARATOR.join(parts)


@dataclass
class _Run:
    calls: dict[str, TaskUpdate] = field(default_factory=dict)  # in the order they started
    ended: list[str] = field(default_factory=list)  # in the order they ended
    shown: str | None = None  # the call shown whole
    summary: str | None = None  # the first card's id
    now: str | None = None  # the second card's id
    split: bool = False  # whether the first card has turned into the counts
    open: bool = True  # whether a new call still joins it


class Fold:
    """Turns the calls of one reply into the cards that show them. `task` returns the cards a
    call's new state changes, in the order they are to appear; `text` ends the run."""

    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}  # call id -> its run
        self._own: dict[str, str] = {}  # call id -> the card that is its own
        self._current: _Run | None = None
        self._sent: dict[str, TaskUpdate] = {}

    def text(self) -> None:
        """Text was written: the calls after it are a new run."""
        if self._current is not None:
            self._current.open = False

    def task(self, update: TaskUpdate) -> list[TaskUpdate]:
        if update.id in self._own:
            return self._changed([replace(update, id=self._own[update.id])])
        run = self._runs.get(update.id)
        if run is None:
            if not folds(update):
                self.text()
                self._own[update.id] = update.id
                return self._changed([update])
            if self._current is None or not self._current.open:
                self._current = _Run()
            run = self._runs[update.id] = self._current
        elif not folds(update):
            return self._changed([self._leave(run, update), *self._draw(run)])
        run.calls[update.id] = update
        if update.status not in ENDED:
            if update.id in run.ended:
                run.ended.remove(update.id)
        elif update.id not in run.ended:
            run.ended.append(update.id)
            # The call that just ended is the one shown, unless another still runs.
            run.shown = update.id
        return self._changed(self._draw(run))

    def _leave(self, run: _Run, update: TaskUpdate) -> TaskUpdate:
        """A call that no longer folds: it keeps the card that shows it, which is its own from
        now on, or gets one. Its run takes no new call."""
        del run.calls[update.id], self._runs[update.id]
        if update.id in run.ended:
            run.ended.remove(update.id)
        run.open = False
        card = update.id
        if run.shown == update.id:
            run.shown = None
            if run.split and run.now is not None:
                card, run.now = run.now, None
            elif not run.split and run.summary is not None:
                card, run.summary = run.summary, None
        self._own[update.id] = card
        return replace(update, id=card)

    def _draw(self, run: _Run) -> list[TaskUpdate]:
        """The run's cards as they stand: the counts of what ended, then the call shown."""
        running = [call.id for call in run.calls.values() if call.status not in ENDED]
        if running:
            run.shown = running[-1]
        shown = run.calls.get(run.shown) if run.shown is not None else None
        counted = [run.calls[i] for i in run.ended if i != run.shown]
        # What replaces the run's cards once the reply's body has ended: every call that ended.
        line = counts([run.calls[i] for i in run.ended], check=True)
        run.split = run.split or bool(counted)
        cards: list[TaskUpdate] = []
        if not run.split:
            if shown is not None:
                run.summary = run.summary or f"fold:{shown.id}"
                cards.append(self._whole(run.summary, shown, line))
            return cards
        if counted:
            run.summary = run.summary or f"fold:{counted[0].id}"
            status: TaskStatus = (
                "complete" if any(call.status == "complete" for call in counted) else "error"
            )
            title = counts(counted, check=False)
            cards.append(TaskUpdate(run.summary, title, status, folded=line))
        if shown is not None:
            run.now = run.now or f"now:{shown.id}"
            cards.append(self._whole(run.now, shown, ""))
        return cards

    @staticmethod
    def _whole(card: str, call: TaskUpdate, line: str) -> TaskUpdate:
        """A call shown whole on a card of its run: its words, and why it failed. `line` is
        what replaces the card once the reply's body has ended; a call still running then has
        no count yet and stays a card."""
        title = call.title
        if call.status == "error" and call.output:
            title += SEPARATOR + call.output
        return TaskUpdate(
            card,
            title,
            call.status,
            name=call.name,
            folded=line if call.status in ENDED else None,
        )

    def _changed(self, cards: list[TaskUpdate]) -> list[TaskUpdate]:
        out = [card for card in cards if self._sent.get(card.id) != card]
        self._sent.update((card.id, card) for card in out)
        return out
