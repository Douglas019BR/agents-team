"""Local state tree (~/.agents-team) and the card's state.json."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import errors

DEFAULT_ROOT = Path("~/.agents-team")


class StateError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Layout:
    root: Path

    @classmethod
    def default(cls, root: Path | str | None = None) -> "Layout":
        return cls(Path(root or os.environ.get("AGENTS_TEAM_HOME") or DEFAULT_ROOT).expanduser().resolve())

    def repo_dir(self, repo_id: str) -> Path:
        return self.root / "repos" / repo_id

    def memory_dir(self, repo_id: str) -> Path:
        return self.repo_dir(repo_id) / "memory"

    def card_dir(self, repo_id: str, card: str) -> Path:
        return self.repo_dir(repo_id) / "cards" / card

    def worktree(self, repo_id: str, card: str) -> Path:
        return self.root / "worktrees" / repo_id / card

    def team_config(self) -> Path:
        return self.root / "config.yml"


def iteration_dir(card_dir: Path, n: int) -> Path:
    return card_dir / "iterations" / f"{n:02d}"


def write_json(path: Path, data: Any) -> None:
    """Atomic write: a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


class CardStore:
    def __init__(self, card_dir: Path):
        self.card_dir = card_dir
        self.path = card_dir / "state.json"

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict[str, Any]:
        state = json.loads(self.path.read_text(encoding="utf-8"))
        problems = errors("state", state)
        if problems:
            raise StateError(f"{self.path}: " + "; ".join(problems))
        return state

    def save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = now()
        problems = errors("state", state)
        if problems:
            raise StateError("refusing to save invalid state: " + "; ".join(problems))
        write_json(self.path, state)

    def transition(
        self,
        state: dict[str, Any],
        to: str,
        *,
        by: str = "orchestrator",
        note: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {"at": now(), "from": state.get("phase"), "to": to,
                                 "iteration": state.get("iteration", 0), "by": by}
        if note:
            entry["note"] = note
        if usage:
            entry["usage"] = usage
            state["usage_total"] = round(state.get("usage_total", 0) + usage.get("credits", 0), 4)
        state["history"].append(entry)
        state["phase"] = to
        if to != "awaiting_human":
            state.pop("awaiting", None)
        self.save(state)
        return state
