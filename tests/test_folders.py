import asyncio
from pathlib import Path
from typing import Any

import pytest

from code_with_slack import texts
from code_with_slack.folders import FOLDER_ROWS, TRUST_BATCH, bind_blocks, bindable_folders


async def trusted_unless_named_untrusted(directory: Path) -> bool:
    return "untrusted" not in directory.name


def tree(root: Path, *relative: str) -> None:
    for rel in relative:
        (root / rel).mkdir(parents=True)


def rows(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in blocks if b.get("block_id", "").startswith("folder-")]


async def test_lists_trusted_folders_two_levels_deep_in_path_order(tmp_path: Path) -> None:
    tree(tmp_path, "b/one/too-deep", "a", "b/two", "b/untrusted", "c-untrusted/inner", ".hidden")
    found = await bindable_folders(tmp_path, trusted_unless_named_untrusted)
    # A trusted folder inside an untrusted one still starts a session, so it is listed.
    listed = [".", "a", "b", "b/one", "b/two", "c-untrusted/inner"]
    assert [p.relative_to(tmp_path).as_posix() for p in found] == listed


async def test_never_descends_into_a_git_repository(tmp_path: Path) -> None:
    # A repository is one project: its src/ and tests/ are not folders to bind on their own.
    tree(tmp_path, "repo/.git", "repo/src", "worktree/src", "plain/sub")
    (tmp_path / "worktree" / ".git").write_text("gitdir: elsewhere\n")  # a worktree's .git
    found = await bindable_folders(tmp_path, trusted_unless_named_untrusted)
    assert [p.relative_to(tmp_path).as_posix() for p in sorted(found)] == [
        ".",
        "plain",
        "plain/sub",
        "repo",
        "worktree",
    ]


async def test_skips_a_symlink_that_would_lead_outside(tmp_path: Path) -> None:
    root = tmp_path / "root"
    tree(tmp_path, "root/app", "outside")
    (root / "link").symlink_to(tmp_path / "outside")
    found = await bindable_folders(root, trusted_unless_named_untrusted)
    assert found == [root, root / "app"]


async def test_stops_one_past_the_rows_shown(tmp_path: Path) -> None:
    checked: list[Path] = []

    async def counting(directory: Path) -> bool:
        checked.append(directory)
        return True

    tree(tmp_path, *(f"f{i:02}" for i in range(FOLDER_ROWS + 10)))
    found = await bindable_folders(tmp_path, counting)
    assert len(found) == FOLDER_ROWS + 1
    assert len(checked) < FOLDER_ROWS + 1 + TRUST_BATCH  # the batch that crossed the line, at most


def test_each_folder_is_a_row_with_a_bind_button_the_current_one_marked(tmp_path: Path) -> None:
    folders = [tmp_path / "a", tmp_path / "b" / "c&d"]
    blocks = bind_blocks(tmp_path, folders, current=tmp_path / "a")
    assert blocks[0]["text"]["text"] == texts.BIND_LIST.format(root=tmp_path)
    first, second = rows(blocks)
    assert first["text"]["text"] == "`a`" + texts.BIND_CURRENT and "accessory" not in first
    assert second["text"]["text"] == "`b/c&amp;d`"
    assert second["accessory"]["action_id"] == "folder_bind"
    assert second["accessory"]["value"] == "b/c&d"


def test_more_folders_than_rows_says_how_to_reach_the_others(tmp_path: Path) -> None:
    folders = [tmp_path / f"f{i:02}" for i in range(FOLDER_ROWS + 1)]
    blocks = bind_blocks(tmp_path, folders, current=None)
    assert len(rows(blocks)) == FOLDER_ROWS
    assert blocks[-1]["text"]["text"] == texts.BIND_MORE.format(rows=FOLDER_ROWS)


def test_no_trusted_folder_says_how_to_trust_one(tmp_path: Path) -> None:
    (only,) = bind_blocks(tmp_path, [], current=None)
    assert only["text"]["text"] == texts.BIND_EMPTY.format(root=tmp_path)


async def test_the_root_is_listed_and_a_repository_root_alone(tmp_path: Path) -> None:
    tree(tmp_path, ".git", "src", "tests")
    assert await bindable_folders(tmp_path, trusted_unless_named_untrusted) == [tmp_path]


async def test_higher_levels_fill_the_rows_first(tmp_path: Path) -> None:
    # A folder with many subfolders must not hide the projects beside it.
    tree(tmp_path, *(f"archive/old{i:02}" for i in range(FOLDER_ROWS + 5)), "work")
    found = await bindable_folders(tmp_path, trusted_unless_named_untrusted)
    shown = [r["accessory"]["value"] for r in rows(bind_blocks(tmp_path, found, current=None))]
    assert "work" in shown and shown == sorted(shown)  # shown in path order all the same


async def test_an_unreadable_folder_is_skipped_not_fatal(tmp_path: Path) -> None:
    tree(tmp_path, "locked", "open")
    (tmp_path / "locked").chmod(0o600)  # listable name, but no stat inside it
    try:
        found = await bindable_folders(tmp_path, trusted_unless_named_untrusted)
    finally:
        (tmp_path / "locked").chmod(0o700)
    assert tmp_path / "open" in found


async def test_trust_is_checked_a_batch_at_a_time(tmp_path: Path) -> None:
    # Each check runs git: in sequence, a root with many untrusted folders is slow to list.
    running, most = 0, 0

    async def slow(directory: Path) -> bool:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return False

    tree(tmp_path, *(f"f{i:02}" for i in range(3 * TRUST_BATCH)))
    assert await bindable_folders(tmp_path, slow) == []
    assert most == TRUST_BATCH


def test_the_root_is_shown_as_written(tmp_path: Path) -> None:
    root = tmp_path / "R&D"
    assert "R&amp;D" in bind_blocks(root, [root / "a"], current=None)[0]["text"]["text"]
    assert "R&amp;D" in bind_blocks(root, [], current=None)[0]["text"]["text"]


async def test_an_unreadable_root_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    # An empty list would tell the owner to trust folders; the real cause is the root.
    root = tmp_path / "root"
    tree(root, "a")
    root.chmod(0o000)
    try:
        with pytest.raises(OSError):
            await bindable_folders(root, trusted_unless_named_untrusted)
    finally:
        root.chmod(0o700)
