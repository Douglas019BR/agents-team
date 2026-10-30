"""Deterministic half of discovery: collect facts, check a proposal, apply it to the repo.

The LLM half (skills/discovery/SKILL.md) reads the repository and proposes
``agents-team.yml`` and ``conventions.md`` in an output directory. This module never guesses:
it reports what is installed, validates the proposal, and copies it only when asked.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import errors, load

SCANNERS = ["semgrep", "gitleaks", "osv-scanner", "trivy", "bandit", "pip-audit"]
TOOLCHAIN = [
    "python3", "uv", "poetry", "pipenv", "pytest", "behave", "tox", "nox", "ruff", "mypy", "pylint",
    "make", "just", "node", "npm", "pnpm", "yarn", "go", "cargo", "mvn", "gradle", "docker",
]
CONTROL_CHARS = re.compile(r"[;&|`$<>\n\r]")
EXCLUDE_ENTRIES = ["/.agents-team.yml", "/.kiro/"]
ROOT_MARKERS = [
    "pyproject.toml", "setup.cfg", "requirements.txt", "Pipfile", "uv.lock", "poetry.lock",
    "behave.ini", "pytest.ini", "tox.ini", "noxfile.py", "Makefile", "justfile", "package.json",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts", "ruff.toml", ".pylintrc",
    "mypy.ini", ".pre-commit-config.yaml", ".gitlab-ci.yml", "Dockerfile", "README.md",
    "CONTRIBUTING.md", "AGENTS.md", "FORGE_GUIDELINES.md", ".agents-team.yml",
]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def repo_id(remote_url: str, fallback: str) -> str:
    """Slug of the remote path: git@host:grp/sub/proj.git -> grp-sub-proj."""
    if not remote_url:
        return re.sub(r"[^a-z0-9._-]+", "-", fallback.lower()).strip("-")
    path = re.sub(r"^[a-z+]+://[^/]+/", "", remote_url)  # https://host/...
    path = re.sub(r"^[^@/]+@[^:]+:", "", path)  # git@host:...
    path = re.sub(r"\.git$", "", path.strip("/"))
    return re.sub(r"[^a-z0-9._-]+", "-", path.lower().replace("/", "-")).strip("-")


def facts(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    remote = _git(repo, "remote", "get-url", "origin")
    head_ref = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return {
        "schema": "agents-team/discovery-facts@1",
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_root": str(repo),
        "repo_id": repo_id(remote, repo.name),
        "remote": _redact(remote),
        "head": _git(repo, "rev-parse", "HEAD"),
        "default_branch": head_ref.removeprefix("origin/") or _git(repo, "branch", "--show-current"),
        "root_files": [m for m in ROOT_MARKERS if (repo / m).exists()],
        "has_features_dir": any(repo.glob("**/*.feature")),
        "scanners": {s: shutil.which(s) is not None for s in SCANNERS},
        "executables": {t: shutil.which(t) is not None for t in TOOLCHAIN},
    }


def _redact(url: str) -> str:
    return re.sub(r"//[^/@]+@", "//***@", url)


def _globs_overlap(repo: Path, a: list[str], b: list[str]) -> set[str]:
    def matched(globs: list[str]) -> set[Path]:
        return {p for g in globs for p in repo.glob(g) if p.is_file()}
    return {str(p.relative_to(repo)) for p in matched(a) & matched(b)}


def check(repo: Path, proposal: Path) -> tuple[list[str], list[str]]:
    """Validate a proposed agents-team.yml. Returns (errors, warnings)."""
    repo = repo.resolve()
    cfg = load(proposal)
    problems = errors("config", cfg)
    if problems:
        return problems, []
    warnings: list[str] = []
    for key, cmd in cfg["commands"].items():
        if CONTROL_CHARS.search(cmd):
            problems.append(f"commands.{key}: shell control characters are not allowed: {cmd!r}")
            continue
        exe = shlex.split(cmd)[0]
        if shutil.which(exe) is None and not (repo / exe).exists():
            problems.append(f"commands.{key}: executable {exe!r} not found on PATH")
    for key, globs in cfg["paths"].items():
        if key in {"features", "steps", "protected"}:
            continue
        if not any(p.is_file() for g in globs for p in repo.glob(g)):
            warnings.append(f"paths.{key}: no file matches {globs}")
    for a, b in (("source", "tests"), ("features", "steps"), ("source", "features")):
        overlap = _globs_overlap(repo, cfg["paths"].get(a, []), cfg["paths"].get(b, []))
        if overlap:
            problems.append(f"paths.{a} and paths.{b} overlap: {sorted(overlap)[:5]}")
    for tool in cfg.get("security", {}).get("tools", []):
        if shutil.which(tool) is None:
            problems.append(f"security.tools: {tool!r} is not installed")
    return problems, warnings


def exclude_file(repo: Path) -> Path:
    common = _git(repo, "rev-parse", "--git-common-dir")
    if not common:
        raise RuntimeError(f"{repo} is not a git repository")
    base = Path(common) if Path(common).is_absolute() else (repo / common)
    return base.resolve() / "info" / "exclude"


def ensure_excluded(repo: Path) -> list[str]:
    """Add agents-team entries to .git/info/exclude (local, never committed). Idempotent."""
    path = exclude_file(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [e for e in EXCLUDE_ENTRIES if e not in current]
    if missing:
        lines = current + (["# agents-team (local only)"] if current else []) + missing
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return missing


def apply(repo: Path, proposal_dir: Path, memory_dir: Path, force: bool = False) -> list[str]:
    """Copy an approved proposal into place. Refuses to overwrite unless ``force``."""
    repo = repo.resolve()
    target = repo / ".agents-team.yml"
    if target.exists() and not force:
        raise FileExistsError(f"{target} exists; pass force=True to replace it")
    problems, _ = check(repo, proposal_dir / "agents-team.yml")
    if problems:
        raise ValueError("proposal is invalid: " + "; ".join(problems))
    done = [f"excluded {e}" for e in ensure_excluded(repo)]
    shutil.copyfile(proposal_dir / "agents-team.yml", target)
    done.append(f"wrote {target}")
    conventions = proposal_dir / "conventions.md"
    if conventions.exists():
        memory_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(conventions, memory_dir / "conventions.md")
        done.append(f"wrote {memory_dir / 'conventions.md'}")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agents-team discovery helpers")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_facts = sub.add_parser("facts", help="write discovery-facts.json")
    p_facts.add_argument("--repo", type=Path, default=Path("."))
    p_facts.add_argument("--out", type=Path, required=True)
    p_check = sub.add_parser("check", help="validate a proposed agents-team.yml")
    p_check.add_argument("--repo", type=Path, default=Path("."))
    p_check.add_argument("--proposal", type=Path, required=True)
    p_apply = sub.add_parser("apply", help="copy an approved proposal into the repo")
    p_apply.add_argument("--repo", type=Path, default=Path("."))
    p_apply.add_argument("--proposal-dir", type=Path, required=True)
    p_apply.add_argument("--memory-dir", type=Path, required=True)
    p_apply.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "facts":
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / "discovery-facts.json"
        path.write_text(json.dumps(facts(args.repo), indent=2) + "\n", encoding="utf-8")
        print(path)
        return 0
    if args.cmd == "check":
        problems, warnings = check(args.repo, args.proposal)
        for w in warnings:
            print(f"warning: {w}")
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1 if problems else 0
    for line in apply(args.repo, args.proposal_dir, args.memory_dir, args.force):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
