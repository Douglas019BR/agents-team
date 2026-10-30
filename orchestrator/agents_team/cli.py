"""agents-team command line: start a card, advance it, approve human gates, show status."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import Engine, EngineError, repo_id_for
from .runner import KiroRunner
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents-team", description=__doc__)
    parser.add_argument("--home", help="state root (default: $AGENTS_TEAM_HOME or ~/.agents-team)")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds per agent run")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="create the card worktree and state")
    p.add_argument("--repo", type=Path, default=Path("."))
    p.add_argument("--card", required=True)
    p.add_argument("--card-file", type=Path, required=True, help="Markdown snapshot of the card")
    p.add_argument("--base", help="base branch (default: current branch of --repo)")
    p.add_argument("--slug")
    for name, help_text in (("run", "advance until a human gate or the end"),
                            ("approve", "approve the pending human gate"),
                            ("retry", "re-run the phase that escalated, after you fixed the cause"),
                            ("status", "show the card state")):
        q = sub.add_parser(name, help=help_text)
        q.add_argument("--repo", type=Path, default=Path("."))
        q.add_argument("--card", required=True)
        if name == "status":
            q.add_argument("--json", action="store_true")
        if name in {"approve", "retry"}:
            q.add_argument("--run", action="store_true", help="continue running afterwards")

    args = parser.parse_args(argv)
    try:
        engine = _engine(args)
        if args.cmd == "start":
            state = engine.start(args.repo, args.card, args.card_file.read_text(encoding="utf-8"), args.base, args.slug)
        else:
            rid = repo_id_for(args.repo)
            if args.cmd == "run":
                state = engine.run(rid, args.card)
            elif args.cmd in {"approve", "retry"}:
                state = engine.approve(rid, args.card) if args.cmd == "approve" else engine.retry(rid, args.card)
                if args.run:
                    state = engine.run(rid, args.card)
            else:
                state = engine.status(rid, args.card)
                if args.json:
                    print(json.dumps(state, indent=2, ensure_ascii=False))
                    return 0
    except EngineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(_summary(state))
    return 1 if state["phase"] in {"escalated", "failed"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
