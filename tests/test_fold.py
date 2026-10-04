from typing import Any

from code_with_slack.render.fold import Fold
from code_with_slack.render.previews import Preview
from code_with_slack.render.renderer import STOPPED, TaskUpdate


def call(id: str, name: str, status: str = "in_progress", **fields: Any) -> TaskUpdate:
    return TaskUpdate(id=id, title=f"{name}: {id}", status=status, name=name, **fields)  # type: ignore[arg-type]


def shown(cards: list[TaskUpdate]) -> list[tuple[str, str, str]]:
    return [(card.id, card.status, card.title) for card in cards]


def test_a_single_call_is_one_card_that_updates_in_place() -> None:
    fold = Fold()
    assert shown(fold.task(call("a", "Bash"))) == [("fold:a", "in_progress", "Bash: a")]
    assert shown(fold.task(call("a", "Bash", "complete"))) == [("fold:a", "complete", "Bash: a")]


def test_the_first_card_turns_into_the_counts_when_a_second_call_is_shown() -> None:
    fold = Fold()
    fold.task(call("a", "Bash"))
    fold.task(call("a", "Bash", "complete"))
    assert shown(fold.task(call("b", "Read"))) == [
        ("fold:a", "complete", "Ran 1 shell command"),
        ("now:b", "in_progress", "Read: b"),
    ]


def test_a_call_that_ended_stays_whole_until_another_takes_its_place() -> None:
    fold = Fold()
    for id, name in (("a", "Bash"), ("b", "Read")):
        fold.task(call(id, name))
        fold.task(call(id, name, "complete"))
    # b ended and was still shown whole: it joins the counts when c takes its place.
    assert shown(fold.task(call("c", "Read"))) == [
        ("fold:a", "complete", "Ran 1 shell command · Read 1 file"),
        ("now:b", "in_progress", "Read: c"),
    ]
    # The first card comes back with the same title: only the line it folds to has changed.
    assert shown(fold.task(call("c", "Read", "complete"))) == [
        ("fold:a", "complete", "Ran 1 shell command · Read 1 file"),
        ("now:b", "complete", "Read: c"),
    ]


def test_a_failed_call_says_why_while_shown_and_is_counted_after_the_cross() -> None:
    fold = Fold()
    fold.task(call("a", "Read"))
    fold.task(call("a", "Read", "complete"))
    fold.task(call("b", "Bash"))
    assert shown(fold.task(call("b", "Bash", "error", output="Exit code 1"))) == [
        ("fold:a", "complete", "Read 1 file"),
        ("now:b", "error", "Bash: b · Exit code 1"),
    ]
    assert shown(fold.task(call("c", "Read"))) == [
        ("fold:a", "complete", "Read 1 file · ✗ Ran 1 shell command"),
        ("now:b", "in_progress", "Read: c"),
    ]


def test_counts_of_failures_alone_show_as_an_error_card() -> None:
    fold = Fold()
    fold.task(call("a", "Bash", "error", output="Exit code 1"))
    cards = fold.task(call("b", "Read"))
    assert shown(cards)[0] == ("fold:a", "error", "✗ Ran 1 shell command")


def test_no_card_of_a_run_carries_details_or_output() -> None:
    fold = Fold()
    cards = fold.task(call("a", "Bash", details="doing"))
    cards += fold.task(call("a", "Bash", "error", output="Exit code 1"))
    cards += fold.task(call("b", "Read"))
    assert all(card.details is None and card.output is None for card in cards)


def test_calls_running_together_show_the_last_one_started_that_still_runs() -> None:
    fold = Fold()
    fold.task(call("a", "Read"))
    assert shown(fold.task(call("b", "Read"))) == [("fold:a", "in_progress", "Read: b")]
    # b ends first: it is counted, and a, still running, is the call shown.
    assert shown(fold.task(call("b", "Read", "complete"))) == [
        ("fold:a", "complete", "Read 1 file"),
        ("now:a", "in_progress", "Read: a"),
    ]
    assert shown(fold.task(call("a", "Read", "complete"))) == [
        ("fold:a", "complete", "Read 1 file"),
        ("now:a", "complete", "Read: a"),
    ]


def test_text_ends_the_run() -> None:
    fold = Fold()
    fold.task(call("a", "Bash", "complete"))
    fold.text()
    assert shown(fold.task(call("b", "Bash"))) == [("fold:b", "in_progress", "Bash: b")]
    # A call of the earlier run still updates that run's card.
    assert fold.task(call("a", "Bash", "complete")) == []


def test_the_folded_line_counts_every_call_that_ended_and_removes_the_second_card() -> None:
    fold = Fold()
    fold.task(call("a", "Bash", "complete"))
    fold.task(call("b", "Read", "complete"))
    cards = fold.task(call("c", "Bash", "error", output="Exit code 1"))
    assert [(card.id, card.folded) for card in cards] == [
        ("fold:a", "✓ Ran 1 shell command · Read 1 file · ✗ Ran 1 shell command"),
        ("now:b", ""),
    ]


def test_a_single_call_folds_to_its_count_and_a_running_one_stays_a_card() -> None:
    fold = Fold()
    assert fold.task(call("a", "Bash"))[0].folded is None
    assert fold.task(call("a", "Bash", "complete"))[0].folded == "✓ Ran 1 shell command"


def test_a_call_with_a_card_of_its_own_passes_through_and_ends_the_run() -> None:
    fold = Fold()
    fold.task(call("a", "Bash", "complete"))
    agent = call("t", "Agent", task=True, details="Read: x")
    assert fold.task(agent) == [agent]
    assert shown(fold.task(call("b", "Bash"))) == [("fold:b", "in_progress", "Bash: b")]


def test_an_edit_that_ends_with_a_preview_keeps_the_card_that_showed_it() -> None:
    fold = Fold()
    fold.task(call("e", "Edit"))
    view = Preview("Update(notes.txt)", "Added 1 line", "1 +x", "diff")
    cards = fold.task(call("e", "Edit", "complete", preview=view))
    assert [(card.id, card.shown_preview, card.folded) for card in cards] == [
        ("fold:e", view, None)
    ]
    # The run is over: the next call starts one of its own.
    assert shown(fold.task(call("b", "Bash"))) == [("fold:b", "in_progress", "Bash: b")]


def test_an_edit_that_arrives_ended_ends_the_run_and_is_not_counted() -> None:
    # Issue #136: the renderer sends an Edit only once it has ended.
    fold = Fold()
    fold.task(call("a", "Bash"))
    fold.task(call("a", "Bash", "complete"))
    view = Preview("Update(notes.txt)", "Added 1 line", "1 +x", "diff")
    done = call("e", "Edit", "complete", preview=view)
    assert fold.task(done) == [done]  # as it came: nothing of the run changes
    assert shown(fold.task(call("b", "Bash"))) == [("fold:b", "in_progress", "Bash: b")]


def test_an_edit_that_arrives_failed_joins_the_run_as_a_call_that_ended() -> None:
    fold = Fold()
    fold.task(call("a", "Bash"))
    fold.task(call("a", "Bash", "complete"))
    cards = fold.task(call("e", "Edit", "error", output="No such file"))
    assert shown(cards) == [
        ("fold:a", "complete", "Ran 1 shell command"),
        ("now:e", "error", "Edit: e · No such file"),
    ]
    assert cards[0].folded == "✓ Ran 1 shell command · ✗ Edit"


def test_a_shown_call_that_becomes_a_task_takes_the_second_card() -> None:
    fold = Fold()
    fold.task(call("a", "Read", "complete"))
    fold.task(call("b", "Bash"))
    background = call("b", "Bash", task=True, details="Running in background")
    cards = fold.task(background)
    assert [(card.id, card.task, card.details) for card in cards] == [
        ("now:b", True, "Running in background")
    ]
    # Later states of that call keep going to the card it took.
    assert [card.id for card in fold.task(call("b", "Bash", "complete", task=True))] == ["now:b"]


def test_a_hidden_call_that_stops_folding_gets_a_card_of_its_own() -> None:
    fold = Fold()
    fold.task(call("a", "Agent"))
    fold.task(call("b", "Read"))
    cards = fold.task(call("a", "Agent", task=True, calls=1))
    assert [card.id for card in cards] == ["a"]


def test_a_stopped_call_keeps_its_card_and_its_word() -> None:
    fold = Fold()
    fold.task(call("a", "Bash"))
    cards = fold.task(call("a", "Bash", "complete", output=STOPPED))
    assert [(card.id, card.output, card.folded) for card in cards] == [("fold:a", STOPPED, None)]


def test_the_calls_left_running_when_the_first_card_is_taken_get_a_new_one() -> None:
    fold = Fold()
    fold.task(call("a", "Read"))
    fold.task(call("b", "Agent"))
    cards = fold.task(call("b", "Agent", task=True, calls=1))
    assert [card.title for card in cards] == ["Agent: b", "Read: a"]
    taken, fresh = (card.id for card in cards)
    assert taken == "fold:a" and fresh != taken  # a card given away is never used again
    # Each call keeps to its own card from here on.
    assert [c.id for c in fold.task(call("b", "Agent", "complete", task=True))] == [taken]
    assert [c.id for c in fold.task(call("a", "Read", "complete"))] == [fresh]


def test_the_call_left_running_when_the_second_card_is_taken_gets_a_new_one() -> None:
    fold = Fold()
    fold.task(call("x", "Read", "complete"))
    fold.task(call("a", "Read"))
    fold.task(call("b", "Bash"))
    cards = fold.task(call("b", "Bash", task=True, details="Running in background"))
    assert (cards[0].id, cards[0].title) == ("now:a", "Bash: b")
    assert cards[-1].title == "Read: a" and cards[-1].id not in ("now:a", "fold:x")
    assert len({card.id for card in cards}) == len(cards)


def test_two_agents_started_together_never_share_a_card() -> None:
    fold = Fold()
    fold.task(call("a", "Agent"))
    fold.task(call("b", "Agent"))
    first = fold.task(call("b", "Agent", task=True, calls=1))
    second = fold.task(call("a", "Agent", task=True, calls=1))
    assert first[0].title == "Agent: b" and second[0].title == "Agent: a"
    assert first[0].id != second[0].id


def test_a_counted_call_that_stops_folding_leaves_the_line_to_the_calls_that_remain() -> None:
    fold = Fold()
    fold.task(call("a", "Agent", "complete"))
    fold.task(call("b", "Read"))
    fold.task(call("a", "Agent", "complete", task=True, calls=1))
    cards = fold.task(call("b", "Read", "complete"))
    assert [(card.title, card.folded) for card in cards] == [
        ("Read: b", "\u2713 Read 1 file"),
        ("Read: b", ""),
    ]
