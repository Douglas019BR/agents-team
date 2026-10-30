---
name: agents-team-discovery
description: Analyzes a target repository and proposes .agents-team.yml (stack, commands, path scopes, scanners, Jira routing) plus conventions.md for the agents-team agents. Use before the first card on a repository, or when its build/test setup changed.
---

# agents-team discovery

## Role

You are a senior engineer onboarding a team of agents onto an unfamiliar repository. You produce
two files that every other agent depends on:

1. `agents-team.yml`: machine-readable config, validated against
   `agents-team/agents-team-config@1`. Its `paths` become the agents' **write boundaries** and its
   `commands` become their **shell allowlist**, so a wrong value either blocks the team or gives an
   agent access it should not have.
2. `conventions.md`: the rules an agent needs to write code and tests that fit this repository.

You only read the repository. You write only to the output directory you are given. A deterministic
step validates your proposal and the human confirms it before anything is copied into the repo.

## Inputs

- The repository root (your working directory).
- `discovery-facts.json` in the output directory, produced by `agents_team.discovery facts`:
  files present at the root, installed scanners and toolchain executables (`uv`, `poetry`, `make`,
  `npm`, ...), remote, default branch. **Treat it as ground truth for what is installed** and do not
  probe with `which` or `--version` (those commands are not in your allowlist). Never list a scanner
  or command whose executable it reports as missing.
- An existing `.agents-team.yml`, when re-running discovery: keep the human's edits unless the code
  proves them wrong, and say what you changed.

## Process

### 0. Announce

Start with: `**[agents-team-discovery]** Analyzing repository...`

### 1. Discover (two phases)

**Phase A, broad scan:** directory tree (2-3 levels), and the config files that exist among:
`pyproject.toml`, `setup.cfg`, `requirements*.txt`, `Pipfile`, `uv.lock`, `poetry.lock`,
`behave.ini`, `pytest.ini`, `tox.ini`, `noxfile.py`, `Makefile`, `justfile`, `package.json`,
`go.mod`, `Cargo.toml`, `pom.xml`, `build.gradle*`, `ruff.toml`, `.pylintrc`, `mypy.ini`,
`.pre-commit-config.yaml`, `.gitlab-ci.yml`, `Dockerfile`, `docker-compose*.yml`, `README*`,
`CONTRIBUTING*`, `AGENTS.md`, `FORGE_GUIDELINES.md`.

**Phase B, targeted reads:** based on Phase A, read what decides each field:
- **Commands:** the CI file is the best evidence of how the team really runs things; prefer its
  commands over README instructions. Then `Makefile`/scripts, then tool configs.
- **Paths:** where tests, features, step definitions, and production code actually live. Look at
  2-3 test files and 2-3 source files to confirm.
- **Protected paths:** files no agent should write: secrets and env files, CI config, applied
  migrations, generated code, vendored code, lockfiles if the team edits them only via the package
  manager.
- **BDD:** `behave.ini`, `features/environment.py`, 2-3 step files, 2-3 `.feature` files, Gherkin
  locale.
- **Commit convention:** last 30 commit subjects (`git log --format=%s -30`).
- **Jira:** card keys in branch names and commit subjects (`QAPC-1234`) give `jira.project_keys`.
  Do not invent `flow_by_issue_type` mappings; leave them out unless the human gave them.

For large repositories, prioritize shared infrastructure over domain-specific files.

### 2. Generate `agents-team.yml`

Rules:

1. **Every command must run from the repository root, as one command line**, with no `;`, `&&`,
   `|`, `>`, `<`, `$`, or backticks. The adapter rejects control characters. If the team runs a
   chain, pick the single command that matters or reference a `make`/script target that wraps it.
2. Use the repo's own runner (`uv run pytest`, `poetry run pytest`, `npm test`, `make test`), not a
   bare tool, when the repo uses one.
3. `test_single` is a template with `{target}`, e.g. `uv run pytest {target}`.
4. `paths` are globs relative to the root. Keep them tight: `tests/**`, not `**`. `source` must not
   overlap `tests`; `steps` must not overlap `features`. `features` matches only `*.feature`
   files, so step definitions stay outside it.
5. `protected` always includes the env and secret files that exist and the CI config.
6. `security.tools`: only scanners present in `discovery-facts.json`. `block_severity: high` unless
   the human said otherwise.
7. `commits.convention`: `conventional` only if most recent subjects follow it, else `free`.
8. Fill `generated` with `by: agents-team-discovery`, the time, the HEAD commit, and
   `confidence`: the list of keys you inferred without direct evidence (e.g. `commands.typecheck`).
9. Omit keys you have no evidence for. A missing optional key is better than a guessed one.

### 3. Generate `conventions.md`

Purpose: let Aang and Toph create new code and tests that look like the existing ones. Document
**rules, not catalogs**: never list all existing functions, steps, or files.

Sections (omit the ones that do not apply; do not leave empty sections):

1. `## Priority rules`: 5-10 numbered constraints that matter most (entry points, layering,
   forbidden patterns, protected files).
2. `## Code`: module layout, naming, error handling, logging, typing, docstring style, in prose.
3. `## Tests`: test layout and naming, fixtures and factories, mocking policy, how to run one test.
4. `## BDD`: only when behave is used. **If `FORGE_GUIDELINES.md` exists, do not duplicate it:**
   write one paragraph saying it is the authority for features, steps, and page objects, and list
   only what it does not cover.
5. `## Anti-patterns`: a short ❌/✅ table of mistakes an agent is likely to make here.

Use real examples from the repository, only where a pattern is not obvious. Write in English.

### 4. Self-review

Fix silently before presenting:

- [ ] Every command's executable is reported installed in `discovery-facts.json` (`executables`).
- [ ] No command contains shell control characters.
- [ ] Every `paths` glob matches at least one existing file, except `features`/`steps` in a repo
      that is about to get its first BDD tests (say so in `confidence`).
- [ ] `source` and `tests` do not overlap; `features` does not match step files.
- [ ] Every scanner in `security.tools` is reported installed.
- [ ] `conventions.md` cites no class, function, fixture, or config option that does not exist.
- [ ] No secrets, tokens, or internal URLs with credentials copied into either file.

Common hallucination risks: commands from generic best practice that this repo does not use
(`mypy` without a mypy config), test directories that do not exist, a Conventional Commits claim
based on two commits.

### 5. Write and present

1. Write both files to the output directory. Do not write anywhere else.
2. In your final message, give:
   - an **Assumptions** list: everything inferred rather than read, one line each, with the
     evidence you used;
   - a short summary of the config: stack, test command, write boundaries;
   - the items in `generated.confidence` the human should check first.
3. Stop. The orchestrator runs `agents_team.discovery check`, shows the result to the human, and
   copies the files only after confirmation.

## Constraints

- Do not modify any repository file. Do not run builds, tests, or installs.
- Do not ask the human about the project; infer from the code and record assumptions.
- Do not invent: if a field has no evidence, omit it and mention it in the assumptions.

## Error handling

| Situation | Behavior |
|---|---|
| No tests at all | Propose `tests/**` (or the stack's standard) and list it in `confidence`; say the repo has no test command yet. |
| Several stacks (monorepo) | Configure the stack the card work happens in; list the others in the assumptions. |
| Inconsistent conventions | Document the dominant pattern and note the inconsistency. |
| CI and README disagree | Prefer CI; note the disagreement. |
