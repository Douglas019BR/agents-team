"""Git operations on the card worktree. Everything is local: nothing here pushes or fetches."""

from __future__ import annotations

import subprocess
from pathlib import Path

ORCHESTRATOR_IDENTITY = ["-c", "user.name=agents-team orchestrator", "-c", "user.email=orchestrator@agents-team.local"]


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def branch_exists(repo: Path, branch: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False) != ""


def add_worktree(repo: Path, path: Path, branch: str, base: str) -> None:
    if branch_exists(repo, branch):
        raise GitError(f"branch {branch} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(path), base)


def changed_files(repo: Path, before: str, after: str = "HEAD") -> list[str]:
    out = git(repo, "diff", "--name-only", f"{before}..{after}")
    return [line for line in out.splitlines() if line]


def commits(repo: Path, before: str, after: str = "HEAD") -> list[tuple[str, str]]:
    out = git(repo, "log", "--format=%H%x09%s", f"{before}..{after}")
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if line]  # type: ignore[misc]


def status(repo: Path) -> tuple[set[str], set[str]]:
    """(tracked files with changes, untracked files). Honors .gitignore and info/exclude."""
    out = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                         capture_output=True, text=True, check=True).stdout
    tracked, untracked = set(), set()
    for line in out.splitlines():
        if not line:
            continue
        path = line[3:].split(" -> ")[-1]
        (untracked if line.startswith("??") else tracked).add(path)
    return tracked, untracked


def dirty_files(repo: Path) -> list[str]:
    tracked, untracked = status(repo)
    return sorted(tracked | untracked)


def commit_files(repo: Path, files: list[str], message: str, as_orchestrator: bool = False) -> str | None:
    """Commit exactly ``files`` (never ``git add -A``, so tool junk is not swept in)."""
    if not files:
        return None
    git(repo, "add", "--", *files)
    if not git(repo, "diff", "--cached", "--name-only"):
        return None
    identity = ORCHESTRATOR_IDENTITY if as_orchestrator else []
    git(repo, *identity, "commit", "-q", "-m", message)
    return head(repo)


def switch_new(repo: Path, branch: str, base: str) -> None:
    if branch_exists(repo, branch):
        raise GitError(f"branch {branch} already exists")
    git(repo, "switch", "-q", "-c", branch, base)


def merge_squash(repo: Path, source: str) -> None:
    git(repo, "merge", "--squash", "-q", source)
