"""No module may build SQL by formatting a string.

Downloaded city data is untrusted (CLAUDE.md, SPEC §3.3), so every value
reaches SQLite as a bound parameter. The one sanctioned exception is sizing an
`IN (...)` clause, which must go through `db.placeholders`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from curbcheck.config import REPO_ROOT

PACKAGE_ROOT = REPO_ROOT / "curbcheck"

# Matched case-sensitively: SQL in this package is written in upper case, and
# lower-case "from"/"where" are ordinary English that appears in log messages.
# This is a tripwire for the review rule, not a SQL parser.
SQL_TEXT = re.compile(
    r"\b(SELECT|INSERT\s+INTO|DELETE\s+FROM|UPDATE|CREATE\s+TABLE|DROP\s+TABLE|ALTER\s+TABLE"
    r"|PRAGMA)\b|\s(FROM|WHERE|VALUES|JOIN)\s"
)


def python_sources() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def looks_like_sql(text: str) -> bool:
    return bool(SQL_TEXT.search(text))


def interpolated_sql_lines(tree: ast.Module) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) and looks_like_sql(_literal_parts(node)):
            lines.append(node.lineno)
    return lines


def percent_formatted_sql_lines(tree: ast.Module) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mod)
            and _is_sql_constant(node.left)
        ):
            lines.append(node.lineno)
    return lines


def formatted_sql_lines(tree: ast.Module) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and _is_sql_constant(node.func.value)
        ):
            lines.append(node.lineno)
    return lines


def unsafe_concatenation_lines(tree: ast.Module) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
            continue
        if _joins_sql(node) and not _calls_placeholders(node):
            lines.append(node.lineno)
    return lines


def test_the_package_has_python_files_to_scan() -> None:
    assert len(python_sources()) > 3


@pytest.mark.parametrize("path", python_sources(), ids=lambda path: path.name)
def test_no_module_builds_sql_with_an_f_string(path: Path) -> None:
    assert interpolated_sql_lines(ast.parse(path.read_text())) == []


@pytest.mark.parametrize("path", python_sources(), ids=lambda path: path.name)
def test_no_module_builds_sql_with_percent_formatting(path: Path) -> None:
    assert percent_formatted_sql_lines(ast.parse(path.read_text())) == []


@pytest.mark.parametrize("path", python_sources(), ids=lambda path: path.name)
def test_no_module_calls_format_on_a_sql_string(path: Path) -> None:
    assert formatted_sql_lines(ast.parse(path.read_text())) == []


@pytest.mark.parametrize("path", python_sources(), ids=lambda path: path.name)
def test_sql_is_only_ever_concatenated_with_placeholders(path: Path) -> None:
    """`... IN (` + `?,?,?` + `)` is the one allowed way to grow a statement."""
    assert unsafe_concatenation_lines(ast.parse(path.read_text())) == []


def test_the_scan_catches_an_interpolated_query() -> None:
    tree = ast.parse('table = "sign"\nquery = f"SELECT * FROM {table}"\n')

    assert interpolated_sql_lines(tree) == [2]


def test_the_scan_catches_a_percent_formatted_query() -> None:
    tree = ast.parse('query = "SELECT * FROM %s" % table\n')

    assert percent_formatted_sql_lines(tree) == [1]


def test_the_scan_catches_a_formatted_query() -> None:
    tree = ast.parse('query = "SELECT * FROM {}".format(table)\n')

    assert formatted_sql_lines(tree) == [1]


def test_the_scan_catches_a_query_concatenated_with_a_value() -> None:
    tree = ast.parse('query = "SELECT * FROM sign WHERE sign_id = " + sign_id\n')

    assert unsafe_concatenation_lines(tree) == [1]


def test_the_scan_allows_an_in_clause_built_from_placeholders() -> None:
    tree = ast.parse('query = "SELECT * FROM sign WHERE sign_id IN (" + placeholders(n) + ")"\n')

    assert unsafe_concatenation_lines(tree) == []


def test_placeholders_is_the_only_helper_that_repeats_a_question_mark() -> None:
    users = [path.name for path in python_sources() if '"?"' in path.read_text()]

    assert users == ["db.py"]


def _literal_parts(node: ast.JoinedStr) -> str:
    return " ".join(
        part.value
        for part in node.values
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )


def _is_sql_constant(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and looks_like_sql(node.value)
    )


def _joins_sql(node: ast.BinOp) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and _is_sql_constant(child):
            return True
        if isinstance(child, ast.Name) and child.id.endswith("_SQL"):
            return True
    return False


def _calls_placeholders(node: ast.BinOp) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "placeholders"
        for child in ast.walk(node)
    )
