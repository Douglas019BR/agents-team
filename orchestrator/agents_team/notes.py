"""AGENT-NOTE comments: render findings into the work branch, find them, strip them."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARKER = "AGENT-NOTE"
NOTE_RE = re.compile(r"AGENT-NOTE\(")

HASH = {".py", ".sh", ".bash", ".rb", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".feature", ".r", ".pl", ".tf", ".mk"}
SLASH = {".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rs",
         ".swift", ".scala", ".php", ".dart", ".groovy", ".gradle"}
DASH = {".sql", ".lua", ".hs"}
HTML = {".html", ".htm", ".xml", ".md", ".vue", ".svelte"}
HASH_NAMES = {"Makefile", "Dockerfile", "Gemfile", "Procfile"}


@dataclass(frozen=True)
class Placement:
    finding_id: str
    file: str
    line: int
    placed: bool
    reason: str = ""


def comment(path: str, text: str) -> str | None:
    name = Path(path).name
    suffix = Path(path).suffix.lower()
    if suffix in HASH or name in HASH_NAMES:
        return f"# {text}"
    if suffix in SLASH:
        return f"// {text}"
    if suffix in DASH:
        return f"-- {text}"
    if suffix in HTML:
        return f"<!-- {text} -->"
    return None


def note_text(finding: dict[str, Any], author: str) -> str:
    message = " ".join(finding["message"].split())
    return f"{MARKER}({finding['id']} {author} -> {finding['owner']}): {message}"


def insert(worktree: Path, findings: list[tuple[dict[str, Any], str]]) -> list[Placement]:
    """Insert one note above each finding's line. ``findings`` pairs a finding with its author."""
    by_file: dict[str, list[tuple[dict[str, Any], str]]] = {}
    result: list[Placement] = []
    for finding, author in findings:
        if not finding.get("file") or not finding.get("line"):
            continue
        by_file.setdefault(finding["file"], []).append((finding, author))
    for rel, items in by_file.items():
        path = (worktree / rel).resolve()
        if worktree.resolve() not in path.parents or not path.is_file():
            result += [Placement(f["id"], rel, f["line"], False, "file not found") for f, _ in items]
            continue
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for finding, author in sorted(items, key=lambda item: item[0]["line"], reverse=True):
            line_no = finding["line"]
            rendered = comment(rel, note_text(finding, author))
            if rendered is None:
                result.append(Placement(finding["id"], rel, line_no, False, "no comment syntax for file type"))
                continue
            if not 1 <= line_no <= len(lines) + 1:
                result.append(Placement(finding["id"], rel, line_no, False, "line out of range"))
                continue
            target = lines[line_no - 1] if line_no <= len(lines) else ""
            indent = target[: len(target) - len(target.lstrip(" \t"))]
            lines.insert(line_no - 1, f"{indent}{rendered}\n")
            result.append(Placement(finding["id"], rel, line_no, True))
        path.write_text("".join(lines), encoding="utf-8")
    return result


def find(worktree: Path, files: list[str] | None = None) -> list[tuple[str, int, str]]:
    """Every AGENT-NOTE line as (file, line number, text)."""
    hits = []
    candidates = files if files is not None else [str(p.relative_to(worktree)) for p in worktree.rglob("*")
                                                   if p.is_file() and ".git" not in p.relative_to(worktree).parts]
    for rel in candidates:
        path = worktree / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if NOTE_RE.search(line):
                hits.append((rel, no, line.strip()))
    return hits


def strip(worktree: Path, files: list[str]) -> int:
    """Remove every line containing an AGENT-NOTE from ``files``. Returns lines removed."""
    removed = 0
    for rel in files:
        path = worktree / rel
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            continue
        kept = [line for line in lines if not NOTE_RE.search(line)]
        if len(kept) != len(lines):
            removed += len(lines) - len(kept)
            path.write_text("".join(kept), encoding="utf-8")
    return removed
