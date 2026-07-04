"""
Tests for scheduled-tasks/workstream-canonicity-check.py.

Behavioral spec (from workstreams-canonicity-model-20260704.md, rules 2 & 3):
- A workstream directory is canonical only if it has both README.md and
  log.md, AND is listed in workstreams/INDEX.md.
- The check is flag-only: it must never move, edit, or delete a workstream
  directory — only report violations.
- `archive/`, `INDEX.md`, and `HOWTO.md` under workstreams/ are not
  themselves workstreams and must never be flagged.
- INDEX.md membership is determined by the first cell of its markdown
  table rows (header and separator rows excluded).

Named after behaviors, not mechanisms.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parents[3] / "scheduled-tasks" / "workstream-canonicity-check.py"
)


def _load_module():
    MODULE_NAME = "workstream_canonicity_check"
    spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec_module so @dataclass can resolve cls.__module__.
    sys.modules[MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


wcc = _load_module()


# ---------------------------------------------------------------------------
# list_workstream_dirs
# ---------------------------------------------------------------------------


def test_list_workstream_dirs_excludes_non_workstream_entries(tmp_path):
    root = tmp_path / "workstreams"
    root.mkdir()
    (root / "wos").mkdir()
    (root / "html-interface").mkdir()
    (root / "archive").mkdir()
    (root / "INDEX.md").write_text("# index")
    (root / "HOWTO.md").write_text("# howto")

    result_names = {p.name for p in wcc.list_workstream_dirs(root)}

    assert result_names == {"wos", "html-interface"}


def test_list_workstream_dirs_returns_empty_when_root_missing(tmp_path):
    root = tmp_path / "does-not-exist"
    assert wcc.list_workstream_dirs(root) == []


# ---------------------------------------------------------------------------
# parse_index_names
# ---------------------------------------------------------------------------


def test_parse_index_names_extracts_first_cell_skipping_header_and_separator():
    index_text = (
        "# Workstreams Index\n\n"
        "| Workstream | Last Active | README | log | Notes |\n"
        "|-----------|------------|--------|-----|-------|\n"
        "| wos | 2026-07-04 | Y | Y | OK — active |\n"
        "| html-interface | 2026-07-04 | Y | Y | OK |\n"
    )

    names = wcc.parse_index_names(index_text)

    assert names == {"wos", "html-interface"}


def test_parse_index_names_returns_empty_set_for_empty_text():
    assert wcc.parse_index_names("") == set()


# ---------------------------------------------------------------------------
# check_workstream
# ---------------------------------------------------------------------------


def test_check_workstream_flags_missing_readme_and_log(tmp_path):
    ws = tmp_path / "some-workstream"
    ws.mkdir()

    flag = wcc.check_workstream(ws, index_names=set())

    assert flag.name == "some-workstream"
    assert flag.missing_readme is True
    assert flag.missing_log is True
    assert flag.not_in_index is True
    assert flag.is_clean is False


def test_check_workstream_is_clean_when_all_rules_satisfied(tmp_path):
    ws = tmp_path / "html-interface"
    ws.mkdir()
    (ws / "README.md").write_text("# html-interface")
    (ws / "log.md").write_text("# log")

    flag = wcc.check_workstream(ws, index_names={"html-interface"})

    assert flag.is_clean is True
    assert flag.missing_readme is False
    assert flag.missing_log is False
    assert flag.not_in_index is False


def test_check_workstream_flags_absent_from_index_even_with_files_present(tmp_path):
    ws = tmp_path / "orphaned-workstream"
    ws.mkdir()
    (ws / "README.md").write_text("# orphaned")
    (ws / "log.md").write_text("# log")

    flag = wcc.check_workstream(ws, index_names={"some-other-workstream"})

    assert flag.missing_readme is False
    assert flag.missing_log is False
    assert flag.not_in_index is True
    assert flag.is_clean is False


# ---------------------------------------------------------------------------
# evaluate_all / flagged_only
# ---------------------------------------------------------------------------


def test_flagged_only_excludes_clean_workstreams(tmp_path):
    clean = tmp_path / "clean-ws"
    clean.mkdir()
    (clean / "README.md").write_text("x")
    (clean / "log.md").write_text("x")

    dirty = tmp_path / "dirty-ws"
    dirty.mkdir()

    results = wcc.evaluate_all([clean, dirty], index_names={"clean-ws", "dirty-ws"})
    flagged = wcc.flagged_only(results)

    assert [f.name for f in flagged] == ["dirty-ws"]


# ---------------------------------------------------------------------------
# render_report — flag-only, never a mutation instruction
# ---------------------------------------------------------------------------


def test_render_report_lists_no_violations_when_all_clean():
    report = wcc.render_report(all_results=[], flagged=[], today="2026-07-04")
    assert "No violations found." in report


def test_render_report_includes_flagged_workstream_and_reasons():
    flag = wcc.WorkstreamFlag(
        name="dirty-ws", missing_readme=True, missing_log=False, not_in_index=True
    )
    report = wcc.render_report(all_results=[flag], flagged=[flag], today="2026-07-04")

    assert "dirty-ws" in report
    assert "flagged 1" in report
    # Flag-only guarantee: report states directories were not mutated, and
    # contains no actionable mutation instruction (e.g. an `rm`/`mv` command).
    assert "no directories were moved, edited, or deleted" in report.lower()
    assert "rm " not in report
    assert "mv " not in report
