"""Runners execute one agent phase in a harness. The engine only talks to this interface."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .adapters import kiro
from .agents import AgentDefinition


@dataclass(frozen=True)
class RunRequest:
    agent: AgentDefinition
    phase: str
    task: str
    ctx: kiro.RunContext
    log_path: Path


@dataclass
class RunResult:
    ok: bool
    output: str = ""
    exit_code: int = 0
    usage: dict[str, Any] = field(default_factory=dict)


class Runner(Protocol):
    def run(self, request: RunRequest) -> RunResult: ...


class KiroRunner:
    """Runs ``kiro-cli chat --no-interactive`` with a per-phase local agent in the worktree."""

    def __init__(self, timeout_secs: int = 1800):
        self.timeout_secs = timeout_secs

    def run(self, request: RunRequest) -> RunResult:
        agents_dir = request.ctx.worktree / ".kiro" / "agents"
        kiro.write_bundle(request.agent, request.phase, request.ctx, agents_dir)
        cmd = kiro.command(request.agent, request.phase, request.ctx, request.task)
        request.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(cmd, cwd=request.ctx.worktree, capture_output=True, text=True,
                                  timeout=self.timeout_secs, stdin=subprocess.DEVNULL)
            output, code = proc.stdout + proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            def text(data: str | bytes | None) -> str:
                return data.decode(errors="replace") if isinstance(data, bytes) else (data or "")
            output = f"{text(exc.stdout)}{text(exc.stderr)}\n[agents-team] timeout after {self.timeout_secs}s"
            code = 124
        request.log_path.write_text(output, encoding="utf-8")
        usage = kiro.parse_usage(output) or {}
        usage["model"] = request.agent.model_for(request.phase, request.ctx.team_models)
        return RunResult(ok=code == 0, output=output, exit_code=code, usage=usage)
