"""Install the human-facing agents (entry agent and Uncle Iroh) into the global Kiro agents dir.

Flow agents are never installed globally: the runner generates them per run in the card worktree.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..agents import load_all
from ..store import Layout
from . import kiro

REPO_ROOT = Path(__file__).resolve().parents[3]
ENTRY_PROMPT = REPO_ROOT / "adapters" / "kiro" / "agents-team-entry.md"
ATLASSIAN_SERVER = "atlassian-rovo-mcp"
JIRA_READ = ["getJiraIssue", "search", "searchJiraIssuesUsingJql", "getAccessibleAtlassianResources",
             "getTransitionsForJiraIssue", "atlassianUserInfo"]
JIRA_WRITE = ["addCommentToJiraIssue", "editJiraIssue", "transitionJiraIssue"]


def _cli_patterns() -> list[str]:
    safe = kiro.SAFE_ARGS
    patterns = [rf"^agents-team {cmd}{safe}$" for cmd in
                ("start", "run", "approve", "retry", "status", "discover", "discover-apply")]
    patterns += [rf"^agents-team --home [^;&|`$<>\n\r ]+ [a-z-]+{safe}$"]
    patterns += [kiro.command_pattern(c) for c in ("git status", "git log", "git branch", "git show", "git diff")]
    return patterns


def entry_config(layout: Layout, prompt_path: Path, atlassian: dict[str, Any] | None) -> dict[str, Any]:
    inbox = layout.root / "inbox"
    config: dict[str, Any] = {
        "name": "agents-team",
        "description": "Front desk for the local agents-team: discovery, start and follow cards, approve gates, "
                       "apply approved Jira actions. Never pushes.",
        "prompt": prompt_path.resolve().as_uri(),
        "tools": [*kiro.READ_TOOLS, "write", "shell"],
        "allowedTools": [*kiro.READ_TOOLS],
        "toolsSettings": {
            "read": {"deniedPaths": list(kiro.SENSITIVE_HOME)},
            "write": {"allowedPaths": [f"{inbox}/**"],
                      "deniedPaths": [f"{layout.root}/repos/**", "**/state.json", *kiro.SENSITIVE_HOME]},
            # Interactive session: anything outside the allowlist asks the human instead of failing.
            "shell": {"allowedCommands": _cli_patterns(), "autoAllowReadonly": True},
        },
        "resources": [f"file://{REPO_ROOT}/README.md"],
        "welcomeMessage": "agents-team pronto. Ex.: 'implementa o QAPC-123 no repositório ~/repos/x' "
                          "ou 'como está o QAPC-123?'.",
    }
    if atlassian:
        config["mcpServers"] = {ATLASSIAN_SERVER: atlassian}
        config["tools"] += [f"@{ATLASSIAN_SERVER}/{t}" for t in JIRA_READ + JIRA_WRITE]
        config["allowedTools"] += [f"@{ATLASSIAN_SERVER}/{t}" for t in JIRA_READ]  # writes always ask
    return config


def install(agents_dir: Path, layout: Layout, source_config: Path | None, register: bool) -> list[str]:
    done: list[str] = []
    agents_dir.mkdir(parents=True, exist_ok=True)
    (layout.root / "inbox").mkdir(parents=True, exist_ok=True)
    (layout.root / "teaching").mkdir(parents=True, exist_ok=True)

    # Entry agent
    prompt = agents_dir / "agents-team.prompt.md"
    prompt.write_text(ENTRY_PROMPT.read_text(encoding="utf-8").format(
        repo_root=REPO_ROOT, state_root=layout.root, inbox=layout.root / "inbox"), encoding="utf-8")
    atlassian = None
    if source_config and source_config.exists():
        atlassian = json.loads(source_config.read_text(encoding="utf-8")).get("mcpServers", {}).get(ATLASSIAN_SERVER)
    entry = agents_dir / "agents-team.json"
    entry.write_text(json.dumps(entry_config(layout, prompt, atlassian), indent=2) + "\n", encoding="utf-8")
    done.append(f"wrote {entry}" + ("" if atlassian else f" (no {ATLASSIAN_SERVER} found: Jira tools disabled)"))

    # Uncle Iroh: read anything in the agents-team repo and the state root, write only teaching/.
    iroh = load_all()["uncle-iroh"]
    ctx = kiro.RunContext(worktree=REPO_ROOT, card_dir=layout.root / "repos", repo_config={},
                          teaching_dir=layout.root / "teaching")
    extra = (f"\n\n## Locations\n\n- agents-team repository: `{REPO_ROOT}`\n- state root: `{layout.root}` "
             f"(cards: `repos/<repo-id>/cards/<CARD>/`; worktrees: `worktrees/`)\n"
             f"- save explanations only to: `{layout.root / 'teaching'}`\n")
    path = kiro.write_bundle(iroh, "teach", ctx, agents_dir, extra_prompt=extra, name="uncle-iroh")
    done.append(f"wrote {path}")
    for legacy in ("at-uncle-iroh-teach.json", "at-uncle-iroh-teach.prompt.md"):  # name used before v0.2
        if (agents_dir / legacy).exists():
            (agents_dir / legacy).unlink()
            done.append(f"removed legacy {agents_dir / legacy}")

    # Dashboard registration. `kirocrew agent create` records source "kirocrew" (the label of
    # KiroCrew's own agent), which hides them among KiroCrew internals. Remove such entries; the
    # dashboard Agents page re-syncs ~/.kiro/agents/*.json as regular (builtin) agents, with the
    # description from the JSON.
    if register:
        if shutil.which("kirocrew") is None:
            done.append("kirocrew not found: skipped dashboard registration cleanup")
        else:
            listed = subprocess.run(["kirocrew", "agent", "list"], capture_output=True, text=True).stdout
            for line in listed.splitlines():
                cols = line.split()
                if len(cols) >= 5 and cols[0] in {"agents-team", "uncle-iroh"} and cols[-1] == "kirocrew":
                    proc = subprocess.run(["kirocrew", "agent", "delete", cols[0]], capture_output=True, text=True)
                    done.append(f"kirocrew agent {cols[0]}: " +
                                ("removed source=kirocrew entry" if proc.returncode == 0 else proc.stderr.strip()))
            done.append("open the dashboard Agents page to sync agents-team and uncle-iroh")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install agents-team's human-facing Kiro agents")
    parser.add_argument("--agents-dir", type=Path, default=Path("~/.kiro/agents").expanduser())
    parser.add_argument("--home", help="state root (default ~/.agents-team)")
    parser.add_argument("--mcp-from", type=Path, default=Path("~/.kiro/agents/kirocrew.json").expanduser(),
                        help=f"agent config to copy the {ATLASSIAN_SERVER} server definition from")
    parser.add_argument("--no-register", action="store_true", help="skip `kirocrew agent create`")
    args = parser.parse_args(argv)
    for line in install(args.agents_dir, Layout.default(args.home), args.mcp_from, not args.no_register):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
