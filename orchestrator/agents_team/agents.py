"""Load canonical agent definitions (agents/<name>.md) and check their consistency."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .schemas import errors

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"
SHARED_FILE = AGENTS_DIR / "_shared.md"


class AgentDefinitionError(ValueError):
    pass


@dataclass(frozen=True)
class AgentDefinition:
    meta: dict[str, Any]
    body: str
    path: Path

    @property
    def name(self) -> str:
        return self.meta["name"]

    @property
    def phases(self) -> list[str]:
        return self.meta["phases"]

    def model_for(self, phase: str, team_models: dict[str, Any] | None = None) -> str:
        """Resolve the model for a phase: team config first, then the definition's default."""
        for source in ((team_models or {}).get(self.name), self.meta.get("default_model")):
            if isinstance(source, str):
                return source
            if isinstance(source, dict) and phase in source:
                return source[phase]
        raise AgentDefinitionError(f"{self.name}: no model for phase '{phase}'")

    def prompt(self) -> str:
        """Full prompt: shared conventions followed by the agent's own instructions."""
        return SHARED_FILE.read_text(encoding="utf-8").rstrip() + "\n\n" + self.body.lstrip()


def parse(path: Path) -> AgentDefinition:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AgentDefinitionError(f"{path.name}: missing YAML frontmatter")
    try:
        _, front, body = text.split("---\n", 2)
    except ValueError as exc:
        raise AgentDefinitionError(f"{path.name}: unterminated frontmatter") from exc
    meta = yaml.safe_load(front) or {}
    problems = errors("agent-definition", meta) + _consistency(meta, path)
    if problems:
        raise AgentDefinitionError(f"{path.name}: " + "; ".join(problems))
    return AgentDefinition(meta=meta, body=body, path=path)


def _consistency(meta: dict[str, Any], path: Path) -> list[str]:
    """Rules JSON Schema cannot express (cross-field references)."""
    problems = []
    phases = set(meta.get("phases", []))
    if meta.get("name") and path.stem != meta["name"]:
        problems.append(f"file name '{path.stem}' differs from name '{meta['name']}'")
    unknown = set(meta.get("permissions", {}).get("write", {})) - phases
    if unknown:
        problems.append(f"write scopes for unknown phases: {sorted(unknown)}")
    default = meta.get("default_model")
    if isinstance(default, dict):
        if set(default) - phases:
            problems.append(f"default_model for unknown phases: {sorted(set(default) - phases)}")
        if phases - set(default):
            problems.append(f"default_model missing phases: {sorted(phases - set(default))}")
    perms = meta.get("permissions", {})
    if any(cmd.startswith("git:commit") for cmd in perms.get("shell", [])) != perms.get("git_commit", False):
        problems.append("git:commit in shell must match git_commit")
    return problems


def load_all(directory: Path = AGENTS_DIR) -> dict[str, AgentDefinition]:
    agents = {}
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("_"):
            continue
        agent = parse(path)
        agents[agent.name] = agent
    return agents
