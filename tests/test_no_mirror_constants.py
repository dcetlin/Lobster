"""
Lint test: detect test files that re-declare production constants as module-level
literals instead of importing or accessing them through the production module.

When a production constant changes, a mirrored test constant silently drifts,
causing assertions to pass against a stale value. This test enforces the rule:
always import or access constants from the production module.

See oracle/learnings.md entries for PRs #696, #800, #967, #970.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent

PRODUCTION_DIRS: Tuple[Path, ...] = (
    REPO_ROOT / "src",
    REPO_ROOT / "scheduled-tasks",
)

TEST_DIR: Path = REPO_ROOT / "tests"

# Minimum 3 characters, all uppercase letters/digits/underscores
ALL_CAPS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

# Names that are intentionally the same across test and production but are not
# mirrors of production values. Add sparingly — prefer fixing the import.
#
# LOBSTER_JID: test files use test-specific JID values ("19995551234@c.us"),
#   while production reads from WHATSAPP_LOBSTER_JID env var. Same name, different purpose.
# ADMIN_CHAT_ID: test files use hardcoded test chat IDs, while production reads
#   from LOBSTER_ADMIN_CHAT_ID env var with a different default. Same name, different purpose.
MIRROR_CONSTANT_ALLOWLIST: frozenset[str] = frozenset({
    "LOBSTER_JID",
    "ADMIN_CHAT_ID",
})


# ---------------------------------------------------------------------------
# AST helpers — pure functions over parsed trees
# ---------------------------------------------------------------------------


def _collect_module_level_constants(
    tree: ast.Module,
) -> Dict[str, int]:
    """Return a mapping of ALL_CAPS names -> line number from module-level assignments.

    Considers both plain assignments (ast.Assign) and annotated assignments
    (ast.AnnAssign, e.g. ``FOO: int = 42``) at the top level — not inside
    classes or functions. Only names matching the ALL_CAPS pattern are collected.
    """
    constants: Dict[str, int] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            # Every target must be a simple ALL_CAPS name
            if not all(
                isinstance(t, ast.Name) and ALL_CAPS_PATTERN.match(t.id)
                for t in node.targets
            ):
                continue
            for target in node.targets:
                assert isinstance(target, ast.Name)
                constants[target.id] = node.lineno
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            # Annotated assignment: ``FOO: int = 42``
            target = node.target
            if isinstance(target, ast.Name) and ALL_CAPS_PATTERN.match(target.id):
                constants[target.id] = node.lineno
    return constants


def _build_production_constant_index(
    production_dirs: Tuple[Path, ...],
) -> Dict[str, List[Tuple[Path, int]]]:
    """Parse all .py files in production directories and collect module-level ALL_CAPS constants.

    Returns a mapping: constant_name -> [(file_path, line_number), ...].
    """
    index: Dict[str, List[Tuple[Path, int]]] = {}
    for prod_dir in production_dirs:
        if not prod_dir.exists():
            continue
        for py_file in sorted(prod_dir.rglob("*.py")):
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError):
                continue
            constants = _collect_module_level_constants(tree)
            for name, lineno in constants.items():
                index.setdefault(name, []).append((py_file, lineno))
    return index


def _scan_test_files_for_violations(
    test_dir: Path,
    production_index: Dict[str, List[Tuple[Path, int]]],
    allowlist: frozenset[str],
    self_path: Path,
) -> List[str]:
    """Scan all test .py files for module-level ALL_CAPS assignments that shadow production constants.

    Returns a list of human-readable violation strings.
    """
    violations: List[str] = []
    if not test_dir.exists():
        return violations

    for test_file in sorted(test_dir.rglob("*.py")):
        # Skip ourselves
        if test_file.resolve() == self_path.resolve():
            continue
        try:
            source = test_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(test_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        test_constants = _collect_module_level_constants(tree)
        for name, test_lineno in sorted(test_constants.items()):
            if name in allowlist:
                continue
            if name not in production_index:
                continue
            # Build the violation message
            prod_locations = production_index[name]
            rel_test = test_file.relative_to(REPO_ROOT)
            prod_lines = "; ".join(
                f"{loc.relative_to(REPO_ROOT)}:{lineno}"
                for loc, lineno in prod_locations
            )
            violations.append(
                f"MIRROR CONSTANT: {rel_test}:{test_lineno} {name}\n"
                f"  Also defined in: {prod_lines}\n"
                f"  Fix: import or access from the production module instead of re-declaring."
            )
    return violations


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_no_mirror_constants():
    """No test file should re-declare a production constant at module level.

    Import or access the constant from the production module instead.
    """
    production_index = _build_production_constant_index(PRODUCTION_DIRS)
    violations = _scan_test_files_for_violations(
        TEST_DIR,
        production_index,
        MIRROR_CONSTANT_ALLOWLIST,
        self_path=Path(__file__),
    )
    assert violations == [], (
        "Test files must not re-declare production constants at module level. "
        "Import or access them from the production module instead.\n\n"
        + "\n\n".join(violations)
    )
