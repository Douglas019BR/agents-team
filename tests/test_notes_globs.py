from pathlib import Path

import pytest

from agents_team import globs, notes


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("src/a.py", "src/**", True),
        ("src/x/y/a.py", "src/**", True),
        ("srcx/a.py", "src/**", False),
        ("features/login.feature", "features/**/*.feature", True),
        ("features/a/b/login.feature", "features/**/*.feature", True),
        ("features/steps/login.py", "features/**/*.feature", False),
        ("tests/test_a.py", "tests/*.py", True),
        ("tests/sub/test_a.py", "tests/*.py", False),
        (".env.local", ".env*", True),
        ("anything/at/all", "**", True),
    ],
)
def test_glob_match(path, pattern, expected):
    assert globs.match(path, pattern) is expected


def _finding(fid, file, line, msg="fix it", owner="aang-dev"):
    return {"id": fid, "file": file, "line": line, "message": msg, "owner": owner}


def test_insert_keeps_indentation_and_line_order(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n    return 2\n")
    placed = notes.insert(tmp_path, [(_finding("R-001", "a.py", 2), "sokka-reviewer"),
                                     (_finding("S-001", "a.py", 3, "no\nnewlines"), "zuko-sec")])
    assert all(p.placed for p in placed)
    assert (tmp_path / "a.py").read_text().splitlines() == [
        "def f():",
        "    # AGENT-NOTE(R-001 sokka-reviewer -> aang-dev): fix it",
        "    return 1",
        "    # AGENT-NOTE(S-001 zuko-sec -> aang-dev): no newlines",
        "    return 2",
    ]


@pytest.mark.parametrize(("name", "start"), [("a.ts", "// "), ("a.sql", "-- "), ("a.md", "<!-- "), ("Makefile", "# ")])
def test_comment_syntax(tmp_path, name, start):
    (tmp_path / name).write_text("x\n")
    notes.insert(tmp_path, [(_finding("R-001", name, 1), "sokka-reviewer")])
    assert (tmp_path / name).read_text().startswith(start + "AGENT-NOTE(")


def test_insert_reports_what_it_could_not_place(tmp_path):
    (tmp_path / "a.bin").write_text("x\n")
    (tmp_path / "a.py").write_text("x\n")
    placed = {p.finding_id: p for p in notes.insert(tmp_path, [
        (_finding("R-001", "missing.py", 1), "s"), (_finding("R-002", "a.bin", 1), "s"),
        (_finding("R-003", "a.py", 99), "s"), (_finding("R-004", "../outside.py", 1), "s")])}
    assert {k: v.reason for k, v in placed.items()} == {
        "R-001": "file not found", "R-002": "no comment syntax for file type",
        "R-003": "line out of range", "R-004": "file not found"}


def test_find_and_strip(tmp_path):
    (tmp_path / "a.py").write_text("a\n# AGENT-NOTE(R-001 s -> a): x\nb\n")
    assert notes.find(tmp_path) == [("a.py", 2, "# AGENT-NOTE(R-001 s -> a): x")]
    assert notes.strip(tmp_path, ["a.py"]) == 1
    assert (tmp_path / "a.py").read_text() == "a\nb\n"
    assert notes.find(tmp_path) == []
