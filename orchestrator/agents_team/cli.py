"""agents-team command line: start a card, advance it, approve human gates, show status."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import discovery, gitops
from .adapters.kiro import RunContext
from .engine import Engine, EngineError, repo_id_for
from .runner import KiroRunner, RunRequest
from .schemas import errors, load
from .store import Layout


def _engine(args: argparse.Namespace) -> Engine:
    layout = Layout.default(args.home)
    team: dict = {}
    if layout.team_config().exists():
        team = load(layout.team_config())
        problems = errors("team-config", team)
        if problems:
            raise EngineError(f"{layout.team_config()} is invalid: " + "; ".join(problems))
    return Engine(layout=layout, runner=KiroRunner(timeout_secs=args.timeout), team=team)


def _summary(state: dict) -> str:
    lines = [f"{state['card']}  phase={state['phase']}  iteration={state['iteration']}  "
             f"credits={state.get('usage_total', 0)}"]
    if state.get("awaiting"):
        lines.append(f"waiting for you: {state['awaiting']['what']} -> {state['awaiting'].get('detail', '')}")
        lines.append(f"then run: agents-team approve --repo <repo> --card {state['card']}")
    if state.get("escalation"):
        lines.append(f"escalated ({state['escalation']['reason']}): {state['escalation'].get('detail', '')}")
    if state["phase"] == "done":
        lines.append(f"official branch {state['branches']['official']} is ready in {state['worktree']}; push it yourself")
    return "\n".join(lines)


def _background(args: argparse.Namespace, engine: Engine, rid: str) -> int:
    """Start ``run`` in a detached process so a chat session is not blocked for minutes."""
    card_dir = engine.layout.card_dir(rid, args.card)
    log = card_dir / "logs" / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "agents_team.cli", "--home", str(engine.layout.root),
           "--timeout", str(args.timeout), "run", "--repo", str(args.repo.resolve()), "--card", args.card]
    with log.open("a", encoding="utf-8") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=str(args.repo.resolve()))
    print(f"{args.card}: running in background (pid {proc.pid}); log {log}")
    print(f"check progress with: agents-team status --repo {args.repo} --card {args.card}")
    return 0


def _discover(args: argparse.Namespace, engine: Engine) -> int:
    """Run the discovery agent: facts -> proposal in the state dir -> deterministic check."""
    repo = args.repo.resolve()
    rid = repo_id_for(repo)
    out = engine.layout.repo_dir(rid) / "discovery"
    out.mkdir(parents=True, exist_ok=True)
    for stale in ("agents-team.yml", "conventions.md"):
        (out / stale).unlink(missing_ok=True)
    facts_path = out / "discovery-facts.json"
    facts_path.write_text(json.dumps(discovery.facts(repo), indent=2) + "\n", encoding="utf-8")
    discovery.ensure_excluded(repo)  # the generated .kiro/ agent must not show up in git status
    agent = engine.agents["discovery"]
    ctx = RunContext(worktree=repo, card_dir=out, repo_config={"paths": {}, "commands": {}},
                     team_models=engine.team.get("models", {}),
                     outputs=(str(out / "agents-team.yml"), str(out / "conventions.md")))
    existing = repo / ".agents-team.yml"
    task = "\n".join([
        "Run discovery.", f"- Repository root (your working directory): {repo}",
        f"- Facts: {facts_path}",
        f"- Existing config (read-only input): {existing if existing.exists() else 'none'}",
        f"- Write exactly: {out / 'agents-team.yml'} and {out / 'conventions.md'}",
        *([f"- Human guidance (treat as a constraint, it overrides your own scoping): {args.focus}"] if args.focus else []),
    ])
    tracked_before = gitops.status(repo)[0]
    result = engine.runner.run(RunRequest(agent, "discover", task, ctx, out / "discovery.log"))
    if gitops.status(repo)[0] != tracked_before:
        print(f"error: discovery changed tracked files in {repo}; inspect `git status` there", file=sys.stderr)
        return 2
    if not result.ok or not (out / "agents-team.yml").exists():
        print(f"error: discovery run failed (exit {result.exit_code}); log {out / 'discovery.log'}", file=sys.stderr)
        return 2
    problems, warnings = discovery.check(repo, out / "agents-team.yml")
    print(f"proposal: {out / 'agents-team.yml'}\nconventions: {out / 'conventions.md'}\n"
          f"log: {out / 'discovery.log'}\ncredits: {result.usage.get('credits', '?')}")
    for w in warnings:
        print(f"warning: {w}")
    for p in problems:
        print(f"error: {p}")
    if problems:
        print("fix the proposal file (or re-run discover) before discover-apply")
        return 1
    print(f"review it, then: agents-team discover-apply --repo {args.repo}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents-team", description=__doc__)
    parser.add_argument("--home", help="state root (default: $AGENTS_TEAM_HOME or ~/.agents-team)")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds per agent run")
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="propose .agents-team.yml for a repository (writes only to the state dir)")
    d.add_argument("--repo", type=Path, default=Path("."))
    d.add_argument("--focus", help="free-text guidance, e.g. 'only the Python harness in integrationtests/'")
    da = sub.add_parser("discover-apply", help="copy the approved proposal into the repository (git-excluded)")
    da.add_argument("--repo", type=Path, default=Path("."))
    da.add_argument("--force", action="store_true", help="replace an existing .agents-team.yml")

    p = sub.add_parser("start", help="create the card worktree and state")
    p.add_argument("--repo", type=Path, default=Path("."))
    p.add_argument("--card", required=True)
    p.add_argument("--card-file", type=Path, required=True, help="Markdown snapshot of the card")
    p.add_argument("--base", help="base branch (default: current branch of --repo)")
    p.add_argument("--slug")
    for name, help_text in (("run", "advance until a human gate or the end"),
                            ("approve", "approve the pending human gate"),
                            ("retry", "re-run the phase that escalated, after you fixed the cause"),
                            ("waive", "mark findings of the current iteration as yours (not the team's) and re-consolidate"),
                            ("status", "show the card state")):
        q = sub.add_parser(name, help=help_text)
        q.add_argument("--repo", type=Path, default=Path("."))
        q.add_argument("--card", required=True)
        if name == "waive":
            q.add_argument("--finding", action="append", required=True, help="finding id, e.g. R-001 (repeatable)")
            q.add_argument("--reason", required=True)
        if name in {"run", "approve", "retry", "waive"}:
            q.add_argument("--background", action="store_true",
                           help="advance in a detached process; follow with `status` (log: <card>/logs/run.log)")
        if name == "status":
            q.add_argument("--json", action="store_true")
        if name in {"approve", "retry", "waive"}:
            q.add_argument("--run", action="store_true", help="continue running afterwards")

    args = parser.parse_args(argv)
    try:
        engine = _engine(args)
        if args.cmd == "discover":
            return _discover(args, engine)
        if args.cmd == "discover-apply":
            repo = args.repo.resolve()
            rid = repo_id_for(repo)
            for line in discovery.apply(repo, engine.layout.repo_dir(rid) / "discovery",
                                        engine.layout.memory_dir(rid), force=args.force):
                print(line)
            return 0
        if args.cmd == "start":
            state = engine.start(args.repo, args.card, args.card_file.read_text(encoding="utf-8"), args.base, args.slug)
        else:
            rid = repo_id_for(args.repo)
            if args.cmd == "run" and args.background:
                return _background(args, engine, rid)
            if args.cmd == "run":
                state = engine.run(rid, args.card)
            elif args.cmd in {"approve", "retry", "waive"}:
                if args.cmd == "approve":
                    state = engine.approve(rid, args.card)
                elif args.cmd == "retry":
                    state = engine.retry(rid, args.card)
                else:
                    state = engine.waive(rid, args.card, args.finding, args.reason)
                if args.background:
                    return _background(args, engine, rid)
                if args.run:
                    state = engine.run(rid, args.card)
            else:
                state = engine.status(rid, args.card)
                if args.json:
                    print(json.dumps(state, indent=2, ensure_ascii=False))
                    return 0
    except (EngineError, FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(_summary(state))
    return 1 if state["phase"] in {"escalated", "failed"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
