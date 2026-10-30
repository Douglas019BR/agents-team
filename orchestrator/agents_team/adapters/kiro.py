"""Kiro CLI adapter.

Generates one Kiro agent config per (agent, phase) run, placed in the card worktree's
``.kiro/agents/`` so Kiro picks it up as a local agent. Permissions rely on behavior verified
against kiro-cli 2.19 in ``--no-interactive`` mode:

- A write outside ``toolsSettings.write.allowedPaths`` is rejected (not prompted).
- ``deniedPaths`` always wins over ``allowedPaths``.
- With ``shell.denyByDefault``, a command that does not fully match ``allowedCommands`` is rejected.
- ``allowedTools`` and ``--trust-tools`` override path/command rules, so ``write`` and ``shell``
  must never be listed there and the runner never passes ``--trust-tools``.
- A rejected call cancels the other calls in the same parallel batch.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents import AgentDefinition, load_all
from ..schemas import load

PREFIX = "at"
READ_TOOLS = ["read", "grep", "glob", "code"]

# Arguments may not contain shell control characters: no chaining, pipes, redirection,
# substitution, or newlines. Each allowed command therefore runs alone.
SAFE_ARG = r"[^;&|`$<>\n\r]"
SAFE_ARGS = rf"([ \t]{SAFE_ARG}*)?"  # not \s: it would accept a newline as the separator

# Never writable by any agent, whatever the definition says.
SENSITIVE_HOME = ["~/.ssh/**", "~/.aws/**", "~/.gnupg/**", "~/.kiro/settings/**", "~/.kiro/agents/**", "~/.config/gh/**"]


class AdapterError(ValueError):
    pass


@dataclass(frozen=True)
class RunContext:
    """Everything that varies per target repository and card.

    ``outputs`` are the exact artifact paths (or globs) this run may write. The ``artifacts``
    write scope resolves to them, never to the whole card directory, so an agent cannot overwrite
    another agent's report.
    """

    worktree: Path
    card_dir: Path
    repo_config: dict[str, Any]
    team_models: dict[str, Any] = field(default_factory=dict)
    teaching_dir: Path | None = None
    outputs: tuple[str, ...] = ()


def agent_name(agent: AgentDefinition, phase: str) -> str:
    return f"{PREFIX}-{agent.name}-{phase.replace('_', '-')}"


def command_pattern(command: str) -> str:
    """Anchored regex for a configured command, allowing only safe extra arguments.

    ``{target}`` in a template becomes a required safe argument.
    """
    if not command.strip():
        raise AdapterError("empty command")
    if re.search(SAFE_ARG.replace("[^", "[", 1), command):
        raise AdapterError(f"command contains shell control characters: {command!r}")
    parts = [re.escape(p) for p in command.strip().split("{target}")]
    return "^" + rf"{SAFE_ARG}+".join(parts) + SAFE_ARGS + "$"


def shell_patterns(agent: AgentDefinition, ctx: RunContext) -> list[str]:
    commands = ctx.repo_config.get("commands", {})
    tools = ctx.repo_config.get("security", {}).get("tools", [])
    patterns: list[str] = []
    for item in agent.meta["permissions"]["shell"]:
        if item.startswith("commands."):
            cmd = commands.get(item.split(".", 1)[1])
            if cmd:
                patterns.append(command_pattern(cmd))
        elif item == "security.tools":
            patterns += [command_pattern(tool) for tool in tools]
        elif item.startswith("git:"):
            patterns.append(command_pattern(f"git {item[4:]}"))
    return list(dict.fromkeys(patterns))


def resolve_scopes(scopes: list[str], ctx: RunContext, allow: bool = True) -> list[str]:
    paths = ctx.repo_config.get("paths", {})
    out: list[str] = []
    for scope in scopes:
        if scope == "repo":
            out.append(f"{ctx.worktree}/**")
        elif scope == "artifacts":
            if allow:
                out += [str(o) for o in ctx.outputs]
            else:
                out.append(f"{ctx.card_dir}/**")
        elif scope == "teaching":
            if ctx.teaching_dir is None:
                raise AdapterError("teaching scope requested but no teaching_dir")
            out.append(f"{ctx.teaching_dir}/**")
        elif scope.startswith("paths."):
            out += [f"{ctx.worktree}/{glob.lstrip('/')}" for glob in paths.get(scope.split(".", 1)[1], [])]
        else:
            raise AdapterError(f"unknown scope {scope!r}")
    return list(dict.fromkeys(out))


def always_denied(ctx: RunContext) -> list[str]:
    return [
        f"{ctx.worktree}/.git/**",
        f"{ctx.worktree}/.kiro/**",
        f"{ctx.worktree}/.agents-team.yml",
        f"{ctx.card_dir}/state.json",
        *SENSITIVE_HOME,
    ]


def render(agent: AgentDefinition, phase: str, ctx: RunContext, prompt_path: Path) -> dict[str, Any]:
    if phase not in agent.phases:
        raise AdapterError(f"{agent.name} has no phase {phase!r}")
    perms = agent.meta["permissions"]
    write_allowed = resolve_scopes(perms["write"].get(phase, []), ctx)
    shell = shell_patterns(agent, ctx)

    tools = list(READ_TOOLS)
    settings: dict[str, Any] = {"read": {"deniedPaths": list(SENSITIVE_HOME)}}
    if write_allowed:
        tools.append("write")
        settings["write"] = {
            "allowedPaths": write_allowed,
            "deniedPaths": resolve_scopes(perms.get("deny_write", []), ctx, allow=False) + always_denied(ctx),
        }
    if shell:
        tools.append("shell")
        settings["shell"] = {"allowedCommands": shell, "denyByDefault": True, "autoAllowReadonly": False}

    return {
        "name": agent_name(agent, phase),
        "description": f"agents-team {agent.meta.get('display_name', agent.name)} ({agent.meta['role']}), phase {phase}",
        "prompt": prompt_path.resolve().as_uri(),
        "model": agent.model_for(phase, ctx.team_models),
        "tools": tools,
        "allowedTools": list(READ_TOOLS),
        "toolsSettings": settings,
        "resources": [],
    }


def render_prompt(agent: AgentDefinition, phase: str) -> str:
    return (
        agent.prompt().rstrip()
        + f"\n\n---\n\nYou are running phase `{phase}` now. Do only that phase, then stop.\n"
    )


def write_bundle(agent: AgentDefinition, phase: str, ctx: RunContext, out_dir: Path) -> Path:
    """Write ``<name>.json`` and ``<name>.prompt.md`` to ``out_dir``; return the JSON path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    name = agent_name(agent, phase)
    prompt_path = out_dir / f"{name}.prompt.md"
    prompt_path.write_text(render_prompt(agent, phase), encoding="utf-8")
    config_path = out_dir / f"{name}.json"
    config = render(agent, phase, ctx, prompt_path)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path


def command(agent: AgentDefinition, phase: str, ctx: RunContext, task: str) -> list[str]:
    """kiro-cli invocation for one phase. Run with ``cwd=ctx.worktree``. No ``--trust-tools``."""
    return [
        "kiro-cli", "chat", "--no-interactive",
        "--agent", agent_name(agent, phase),
        "--model", agent.model_for(phase, ctx.team_models),
        task,
    ]


USAGE_RE = re.compile(r"Credits:\s*([0-9]+(?:\.[0-9]+)?)\s*\W+\s*Time:\s*([0-9]+(?:\.[0-9]+)?)s")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def parse_usage(output: str) -> dict[str, float] | None:
    """Credits and wall time from kiro-cli's trailing ``Credits: X • Time: Ys`` line.

    kiro-cli exposes credits per run but no token counts. When a run prints several
    summaries, the last one is the run total.
    """
    matches = USAGE_RE.findall(ANSI_RE.sub("", output))
    if not matches:
        return None
    credits, seconds = matches[-1]
    return {"credits": float(credits), "seconds": float(seconds)}


def validate_with_kiro(config_path: Path) -> tuple[bool, str]:
    if shutil.which("kiro-cli") is None:
        return False, "kiro-cli not found"
    proc = subprocess.run(
        ["kiro-cli", "agent", "validate", "--path", str(config_path)],
        capture_output=True, text=True, timeout=60,
    )
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0 and "error" not in output.lower(), output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Kiro agent configs for every agent phase.")
    parser.add_argument("--config", type=Path, required=True, help=".agents-team.yml of the target repo")
    parser.add_argument("--team-config", type=Path, help="~/.agents-team/config.yml")
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--card-dir", type=Path, required=True)
    parser.add_argument("--teaching-dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--validate", action="store_true", help="run kiro-cli agent validate on each file")
    args = parser.parse_args(argv)

    team = load(args.team_config) if args.team_config else {}
    ctx = RunContext(
        worktree=args.worktree.resolve(),
        card_dir=args.card_dir.resolve(),
        repo_config=load(args.config),
        team_models=team.get("models", {}),
        teaching_dir=args.teaching_dir.resolve() if args.teaching_dir else None,
        outputs=(f"{args.card_dir.resolve()}/**",),  # preview only; real runs pass exact files
    )
    failed = 0
    for agent in load_all().values():
        for phase in agent.phases:
            if "teaching" in agent.meta["permissions"]["write"].get(phase, []) and ctx.teaching_dir is None:
                print(f"skip  {agent_name(agent, phase)} (no --teaching-dir)")
                continue
            path = write_bundle(agent, phase, ctx, args.out)
            status = "wrote"
            if args.validate:
                ok, output = validate_with_kiro(path)
                status = "ok   " if ok else "FAIL "
                failed += not ok
                if not ok:
                    print(output, file=sys.stderr)
            print(f"{status} {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
