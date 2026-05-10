"""
Tests for nested subtasks (parent_id) feature.

Covers:
- render_active_todos() renders subtasks indented under parents
- render_active_todos() excludes subtasks from top-level rendering
- Sync correctly parses indented [ ] as new subtask insert with correct parent_id
- Sync correctly marks indented [x] as done
- handle_todo_add with --parent flag — success, non-existent parent error, depth-cap error
"""
from __future__ import annotations

import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_TASKS_DIR = _REPO_ROOT / "scheduled-tasks"
if str(_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(_TASKS_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.los.db import (
    ActionItemStatus,
    connect,
    get_item_by_id,
    get_open_items,
    get_subtasks,
    insert_action_item,
    mark_done,
)
from src.los.todo_commands import handle_todo_add, route_todo_command
from todo_obsidian_sync import (
    parse_active_todos,
    render_active_todos,
    sync_obsidian_to_db,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = connect(tmp_path / "test.db")
    yield db
    db.close()


@pytest.fixture
def chat_id() -> int:
    return 8075091586


# ---------------------------------------------------------------------------
# render_active_todos — subtask rendering
# ---------------------------------------------------------------------------


def test_render_subtasks_indented_under_parent(conn: sqlite3.Connection) -> None:
    """Subtasks must render with 2-space indent immediately after their parent."""
    parent_id = insert_action_item(conn, text="Fix the sync bug", source="telegram", source_message_id=None)
    insert_action_item(conn, text="Write failing test", source="telegram", source_message_id=None, parent_id=parent_id)

    output = render_active_todos(conn)

    lines = output.splitlines()
    parent_line_idx = next(i for i, l in enumerate(lines) if "Fix the sync bug" in l)
    subtask_line_idx = next(i for i, l in enumerate(lines) if "Write failing test" in l)

    # Subtask must come right after parent
    assert subtask_line_idx == parent_line_idx + 1
    # Subtask must be indented with 2 spaces
    subtask_line = lines[subtask_line_idx]
    assert subtask_line.startswith("  - [ ]"), f"Expected 2-space indent, got: {subtask_line!r}"


def test_render_subtasks_have_parent_comment(conn: sqlite3.Connection) -> None:
    """Subtask lines must include <!-- id:N parent:P --> comment."""
    parent_id = insert_action_item(conn, text="Parent task", source="telegram", source_message_id=None)
    sub_id = insert_action_item(conn, text="Child task", source="telegram", source_message_id=None, parent_id=parent_id)

    output = render_active_todos(conn)

    # Find the subtask line
    subtask_lines = [l for l in output.splitlines() if "Child task" in l]
    assert len(subtask_lines) == 1
    line = subtask_lines[0]
    assert f"<!-- id:{sub_id} parent:{parent_id} -->" in line, (
        f"Subtask line missing id/parent comment: {line!r}"
    )


def test_render_top_level_items_have_id_comment(conn: sqlite3.Connection) -> None:
    """Top-level item lines must include <!-- id:N --> comment."""
    item_id = insert_action_item(conn, text="Top level task", source="telegram", source_message_id=None)

    output = render_active_todos(conn)

    item_lines = [l for l in output.splitlines() if "Top level task" in l]
    assert len(item_lines) == 1
    line = item_lines[0]
    assert f"<!-- id:{item_id} -->" in line, f"Top-level line missing id comment: {line!r}"
    # Must NOT have parent comment
    assert "parent:" not in line


def test_render_excludes_subtasks_from_top_level(conn: sqlite3.Connection) -> None:
    """Subtasks must not appear as independent top-level items."""
    parent_id = insert_action_item(conn, text="Parent task", source="telegram", source_message_id=None)
    insert_action_item(conn, text="Child task", source="telegram", source_message_id=None, parent_id=parent_id)

    output = render_active_todos(conn)

    # Child task must only appear once (as subtask under parent, not also at top level)
    occurrences = output.count("Child task")
    assert occurrences == 1, f"Child task appeared {occurrences} times, expected 1"

    # The one occurrence must be indented
    child_lines = [l for l in output.splitlines() if "Child task" in l]
    assert child_lines[0].startswith("  - [ ]")


def test_render_multiple_subtasks_under_same_parent(conn: sqlite3.Connection) -> None:
    """Multiple subtasks under the same parent must all render consecutively."""
    parent_id = insert_action_item(conn, text="Big feature", source="telegram", source_message_id=None)
    insert_action_item(conn, text="Step one", source="telegram", source_message_id=None, parent_id=parent_id)
    insert_action_item(conn, text="Step two", source="telegram", source_message_id=None, parent_id=parent_id)
    insert_action_item(conn, text="Step three", source="telegram", source_message_id=None, parent_id=parent_id)

    output = render_active_todos(conn)
    lines = output.splitlines()

    parent_idx = next(i for i, l in enumerate(lines) if "Big feature" in l)
    step_lines = [l for l in lines[parent_idx + 1:parent_idx + 4] if l.startswith("  - [ ]")]
    assert len(step_lines) == 3


def test_render_done_subtasks_are_excluded(conn: sqlite3.Connection) -> None:
    """Done subtasks must not appear in render output."""
    parent_id = insert_action_item(conn, text="Parent", source="telegram", source_message_id=None)
    sub_id = insert_action_item(conn, text="Done subtask", source="telegram", source_message_id=None, parent_id=parent_id)
    mark_done(conn, sub_id)

    output = render_active_todos(conn)
    assert "Done subtask" not in output


def test_render_is_stable(conn: sqlite3.Connection) -> None:
    """Same DB state always produces identical render output."""
    parent_id = insert_action_item(conn, text="Stable parent", source="telegram", source_message_id=None)
    insert_action_item(conn, text="Stable subtask", source="telegram", source_message_id=None, parent_id=parent_id)

    output1 = render_active_todos(conn)
    output2 = render_active_todos(conn)
    assert output1 == output2


# ---------------------------------------------------------------------------
# parse_active_todos — subtask parsing
# ---------------------------------------------------------------------------


SUBTASK_TODOS = textwrap.dedent("""\
    # ✅ ACTIVE TODOS
    *Generated by LOS — 2 open items*

    ## Active (P4–P6)

    ### general
    - [ ] Fix the sync bug <!-- id:42 -->
      - [ ] Write failing test <!-- id:43 parent:42 -->
      - [x] Old subtask done <!-- id:44 parent:42 -->
    - [ ] New parent task <!-- id:99 -->
      - [ ] New subtask without id in db

    ## Someday / Aspirational (P7–P9)
    *(none)*

    ---
    *To mark done, dismiss, or snooze: tell Lobster via Telegram, or check the box in Obsidian.*
""")


def test_parse_indented_checkbox_as_subtask() -> None:
    """2-space-indented '- [ ]' lines must be parsed as subtasks (parent_id set)."""
    result = parse_active_todos(SUBTASK_TODOS)
    subtasks = [item for item in result.open if item.parent_id is not None]
    assert len(subtasks) >= 1
    texts = [s.text for s in subtasks]
    assert "Write failing test" in texts


def test_parse_subtask_extracts_parent_id() -> None:
    """Subtask items must have parent_id extracted from <!-- parent:N --> comment."""
    result = parse_active_todos(SUBTASK_TODOS)
    subtask = next(item for item in result.open if item.text == "Write failing test")
    assert subtask.parent_id == 42


def test_parse_subtask_extracts_item_id() -> None:
    """Subtask items must have item_id extracted from <!-- id:N --> comment."""
    result = parse_active_todos(SUBTASK_TODOS)
    subtask = next(item for item in result.open if item.text == "Write failing test")
    assert subtask.item_id == 43


def test_parse_done_subtask_goes_to_done_list() -> None:
    """Indented '- [x]' lines must be parsed into the done list."""
    result = parse_active_todos(SUBTASK_TODOS)
    done_texts = [item.text for item in result.done]
    assert "Old subtask done" in done_texts


def test_parse_done_subtask_has_parent_id() -> None:
    """Done subtask must have parent_id set."""
    result = parse_active_todos(SUBTASK_TODOS)
    done_sub = next(item for item in result.done if item.text == "Old subtask done")
    assert done_sub.parent_id == 42  # parent:42 from comment


def test_parse_top_level_items_have_no_parent_id() -> None:
    """Top-level (unindented) items must have parent_id=None."""
    result = parse_active_todos(SUBTASK_TODOS)
    top_level = [item for item in result.open if item.parent_id is None]
    texts = [i.text for i in top_level]
    assert "Fix the sync bug" in texts
    assert "New parent task" in texts


def test_parse_strips_html_comment_from_text() -> None:
    """Item text must not include HTML comments."""
    result = parse_active_todos(SUBTASK_TODOS)
    for item in result.open + result.done:
        assert "<!--" not in item.text, f"HTML comment not stripped from: {item.text!r}"
        assert "-->" not in item.text


# ---------------------------------------------------------------------------
# sync_obsidian_to_db — subtask sync
# ---------------------------------------------------------------------------


def test_sync_inserts_new_subtask_with_parent_id(conn: sqlite3.Connection) -> None:
    """New indented [ ] items in the file must be inserted with parent_id set."""
    # Pre-insert the parent so its dedup_key exists
    parent_db_id = insert_action_item(conn, text="Fix the sync bug", source="telegram", source_message_id=None)

    content = textwrap.dedent("""\
        ## Active (P4–P6)

        ### general
        - [ ] Fix the sync bug <!-- id:{pid} -->
          - [ ] Brand new subtask
    """).format(pid=parent_db_id)

    sync_obsidian_to_db(conn, content)

    # Find "Brand new subtask" in DB
    cur = conn.execute("SELECT id, parent_id FROM action_items WHERE text = 'Brand new subtask'")
    row = cur.fetchone()
    assert row is not None, "Brand new subtask was not inserted"
    # The parent_id in the inserted row should match the parent that was in the file
    assert row[1] is not None, "Subtask parent_id should be set"


def test_sync_marks_done_indented_checked_item(conn: sqlite3.Connection) -> None:
    """Indented [x] items must be marked done in DB."""
    parent_id = insert_action_item(conn, text="Parent task", source="telegram", source_message_id=None)
    sub_id = insert_action_item(conn, text="Done child", source="telegram", source_message_id=None, parent_id=parent_id)

    content = textwrap.dedent("""\
        ## Active (P4–P6)

        ### general
        - [ ] Parent task <!-- id:{pid} -->
          - [x] Done child <!-- id:{sid} parent:{pid} -->
    """).format(pid=parent_id, sid=sub_id)

    sync_obsidian_to_db(conn, content)

    item = get_item_by_id(conn, sub_id)
    assert item.status == ActionItemStatus.DONE


# ---------------------------------------------------------------------------
# handle_todo_add — --parent flag
# ---------------------------------------------------------------------------


def test_add_with_parent_inserts_subtask(conn: sqlite3.Connection, chat_id: int) -> None:
    """'/todo add text --parent N' must insert item with parent_id=N."""
    parent_id = insert_action_item(conn, text="Parent task", source="telegram", source_message_id=None)

    reply = handle_todo_add("Subtask text", chat_id=chat_id, source="telegram", parent_id=parent_id, conn=conn)

    cur = conn.execute("SELECT parent_id FROM action_items WHERE text = 'Subtask text'")
    row = cur.fetchone()
    assert row is not None
    assert row[0] == parent_id
    assert str(parent_id) in reply


def test_add_with_nonexistent_parent_returns_error(conn: sqlite3.Connection, chat_id: int) -> None:
    """If --parent refers to a non-existent item, return error message."""
    reply = handle_todo_add("Some task", chat_id=chat_id, source="telegram", parent_id=99999, conn=conn)

    assert "No item with ID" in reply or "99999" in reply


def test_add_with_subtask_as_parent_returns_depth_error(conn: sqlite3.Connection, chat_id: int) -> None:
    """If --parent refers to an item that already has a parent_id, return depth-cap error."""
    grandparent_id = insert_action_item(conn, text="Grandparent", source="telegram", source_message_id=None)
    parent_id = insert_action_item(conn, text="Parent", source="telegram", source_message_id=None, parent_id=grandparent_id)

    reply = handle_todo_add("Grandchild", chat_id=chat_id, source="telegram", parent_id=parent_id, conn=conn)

    assert "2 levels" in reply or "Cannot nest" in reply


def test_add_without_parent_works_as_before(conn: sqlite3.Connection, chat_id: int) -> None:
    """/todo add without --parent inserts top-level item (parent_id=None)."""
    handle_todo_add("Top level item", chat_id=chat_id, source="telegram", conn=conn)

    items = get_open_items(conn)
    assert len(items) == 1
    assert items[0].parent_id is None


def test_route_todo_add_with_parent_flag(conn: sqlite3.Connection, chat_id: int) -> None:
    """'/todo add text --parent N' via route_todo_command must insert subtask."""
    parent_id = insert_action_item(conn, text="Big project", source="telegram", source_message_id=None)

    msg = {"text": f"/todo add First step --parent {parent_id}", "chat_id": chat_id, "source": "telegram"}
    reply = route_todo_command(msg, conn=conn)

    cur = conn.execute("SELECT parent_id FROM action_items WHERE text = 'First step'")
    row = cur.fetchone()
    assert row is not None
    assert row[0] == parent_id
    assert isinstance(reply, str)
    assert len(reply) > 0


def test_route_todo_add_without_parent_still_works(conn: sqlite3.Connection, chat_id: int) -> None:
    """Existing /todo add without --parent must still work (backward compat)."""
    msg = {"text": "/todo add No parent here", "chat_id": chat_id, "source": "telegram"}
    route_todo_command(msg, conn=conn)

    items = get_open_items(conn)
    assert len(items) == 1
    assert items[0].text == "No parent here"
    assert items[0].parent_id is None
