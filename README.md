# agents-team

Local team of engineering agents that works a Jira card end to end on your machine and hands you a
clean branch to push. Harness-portable by design; the first adapter is Kiro / KiroCrew.

| Agent      | Role      | Model (default)                       | Writes                          |
|------------|-----------|---------------------------------------|---------------------------------|
| Aang       | dev       | claude-opus-5.5 (plan), glm-5 (code)  | plan, production code, steps    |
| Toph       | SDET      | claude-sonnet-5.5                     | tests, `.feature` files         |
| Sokka      | reviewer  | gpt-5.6-terra                         | review reports only             |
| Katara     | QA        | claude-haiku-5.5                      | QA reports only                 |
| Zuko       | sec       | gpt-5.6-luna                          | security reports only           |
| Uncle Iroh | teacher   | claude-opus-5.5                       | `teaching/` only, on request    |

Nothing leaves your machine: agents commit to a local work branch, the orchestrator assembles an
official branch, and **you** push and open the MR. Design and decisions: [`docs/design.md`](docs/design.md).

## Status

- Flow `implement-system`: implemented and tested end to end with real agents.
- Flows `bdd-behave` and `peer-review`: agents defined, orchestration not implemented yet.
- Jira: the dashboard agent reads cards and applies **approved** actions through the Atlassian MCP;
  the CLI only exports them.

## Install (done once)

```bash
uv tool install -e .                                # puts `agents-team` on PATH
uv run python -m agents_team.adapters.kiro_install  # dashboard agents: agents-team, uncle-iroh
```

Models and limits: `~/.agents-team/config.yml` (schema `schemas/team-config.schema.json`).

## Use from the KiroCrew dashboard

Pick the **agents-team** agent and ask, in Portuguese:

- "prepara o repositório ~/repos/x" → discovery proposal, you confirm, it applies.
- "implementa o QAPC-123 no repositório ~/repos/x" → reads the card from Jira, starts and runs in background.
- "como está o QAPC-123?" → status, what is waiting for you.
- "aprova" (plan / grouping / Jira actions) → continues; Jira writes ask for your approval one by one.

Pick **uncle-iroh** to ask how anything works ("por que o Zuko bloqueou?", "explica o state.json").

## Use from the terminal

```bash
agents-team discover --repo ~/repos/x          # proposal in ~/.agents-team/repos/<id>/discovery/
agents-team discover-apply --repo ~/repos/x    # copies .agents-team.yml (git-excluded)
agents-team start --repo ~/repos/x --card QAPC-123 --card-file card.md [--base develop]
agents-team run --repo ~/repos/x --card QAPC-123 [--background]
agents-team status --repo ~/repos/x --card QAPC-123 [--json]
agents-team approve --repo ~/repos/x --card QAPC-123 [--run | --background]
agents-team retry --repo ~/repos/x --card QAPC-123 [--run | --background]
```

Where things are, per card (`~/.agents-team/repos/<repo-id>/cards/<CARD>/`):
`state.json` (phase, verdicts, credits per run), `plan.md`, `test-plan.md`, `iterations/NN/*.json`,
`grouping-plan.md` (edit the commit message before approving), `final-report.md`, `logs/`.
Worktree: `~/.agents-team/worktrees/<repo-id>/<CARD>`.

## Development

```bash
uv run pytest
uv run python -m agents_team.adapters.kiro --config schemas/examples/agents-team.yml \
  --worktree /tmp/wt --card-dir /tmp/card --out build/kiro --validate   # preview generated agents
```
