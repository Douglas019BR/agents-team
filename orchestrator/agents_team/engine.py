"""State machine for the ``implement-system`` flow.

One call to :meth:`Engine.run` advances the card until it needs a human (``awaiting_human``) or
reaches a terminal phase. Every agent run is checked by the orchestrator before its result counts:

- outputs exist and validate (one retry with the errors, then escalation);
- no other artifact in the card directory changed (an agent cannot edit another agent's report
  or ``state.json``);
- repository changes stay inside the agent's write scope; read-only agents change nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

from . import discovery, globs, gitops, notes
from .adapters.kiro import RunContext
from .agents import AgentDefinition, load_all
from .runner import RunRequest, Runner
from .schemas import _registry, errors, load
from .store import CardStore, Layout, iteration_dir, now, write_json

FLOW = "implement-system"
TERMINAL = {"done", "escalated", "failed"}
# Generated files that must never be committed on an agent's behalf, even inside its write scope
# and even when the target repo lacks a .gitignore for them.
JUNK = ["**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**", "**/.mypy_cache/**", "**/.ruff_cache/**",
        "**/node_modules/**", "**/.coverage", "**/coverage.xml", "**/htmlcov/**", "**/.DS_Store",
        "**/*.egg-info/**", "**/.tox/**", "**/.nox/**", "**/dist/**", "**/build/**", "**/target/**",
        "**/.kiro/**", "**/.venv/**"]
REPORT_AUTHORS = {"review.json": "sokka-reviewer", "qa.json": "katara-qa", "sec.json": "zuko-sec"}
EMPTY_SECTION = {"", "none", "n/a", "-", "nenhum", "nenhuma", "não há", "nao ha"}
_JIRA_ACTION = Draft202012Validator(
    {"$ref": "https://schemas.agents-team.dev/v1/common.schema.json#/$defs/jira_action"}, registry=_registry()
)


class EngineError(RuntimeError):
    pass


class Escalate(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


# --- helpers -----------------------------------------------------------------------------------

def slugify(text: str, limit: int = 40) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "card"


def card_title(card_md: str, fallback: str) -> str:
    for line in card_md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def section(markdown: str, heading: str) -> str:
    """Body of a ``## heading`` section, up to the next ``## ``."""
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", markdown, re.M | re.S | re.I)
    return match.group(1).strip() if match else ""


def has_content(text: str) -> bool:
    """True when the section lists at least one item (``- x``, ``* x`` or ``1. x``).

    Free prose such as "Nenhum teste contestado." does not count: agents are told to list
    disputes as bullets, so only bullets are treated as disputes.
    """
    items = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", l).strip().lower()
             for l in text.splitlines() if re.match(r"^\s*(?:[-*]|\d+[.)])\s+\S", l)]
    return any(i.rstrip(".") not in EMPTY_SECTION for i in items)


def markdown_jira_actions(markdown: str) -> list[Any]:
    body = section(markdown, "Jira actions")
    match = re.search(r"```json\s*(.*?)```", body, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _digest(card_dir: Path, skip: Callable[[Path], bool]) -> dict[str, str]:
    out = {}
    for path in sorted(card_dir.rglob("*")):
        if path.is_file() and not skip(path):
            out[str(path.relative_to(card_dir))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def repo_id_for(repo: Path) -> str:
    remote = gitops.git(repo, "remote", "get-url", "origin", check=False)
    return discovery.repo_id(remote, repo.resolve().name)


# --- engine ------------------------------------------------------------------------------------

@dataclass
class Engine:
    layout: Layout
    runner: Runner
    agents: dict[str, AgentDefinition] = field(default_factory=load_all)
    team: dict[str, Any] = field(default_factory=dict)
    gate_timeout_secs: int = 1800

    # public API ---------------------------------------------------------------------------

    def start(self, repo: Path, card: str, card_md: str, base: str | None = None, slug: str | None = None) -> dict[str, Any]:
        repo = repo.resolve()
        cfg_path = repo / ".agents-team.yml"
        if not cfg_path.exists():
            raise EngineError(f"{cfg_path} not found: run discovery first")
        cfg = load(cfg_path)
        problems = errors("config", cfg)
        if problems:
            raise EngineError(f"{cfg_path} is invalid: " + "; ".join(problems))
        rid = repo_id_for(repo)
        card_dir = self.layout.card_dir(rid, card)
        store = CardStore(card_dir)
        if store.exists():
            raise EngineError(f"card {card} already started at {card_dir}")
        base = base or cfg.get("branches", {}).get("base") or gitops.git(repo, "branch", "--show-current") or "main"
        title = card_title(card_md, card)
        official_pattern = cfg.get("branches", {}).get("official", "feature/{card}-{slug}")
        official = official_pattern.format(card=card, slug=slug or slugify(title)).strip("-")
        work = f"agents/{card}/work"
        if official == work or official.startswith(f"{work}/") or work.startswith(f"{official}/"):
            raise EngineError(f"official branch {official!r} conflicts with work branch {work!r}")
        if not gitops.branch_exists(repo, base):
            raise EngineError(f"base branch {base!r} does not exist in {repo}")
        if gitops.branch_exists(repo, official):
            raise EngineError(f"official branch {official!r} already exists in {repo}; pick another with `branches.official` or --slug")
        worktree = self.layout.worktree(rid, card)

        card_dir.mkdir(parents=True, exist_ok=True)
        (card_dir / "card.md").write_text(card_md, encoding="utf-8")
        shutil.copyfile(cfg_path, card_dir / "agents-team.yml")  # snapshot: stable for the whole card
        discovery.ensure_excluded(repo)
        gitops.add_worktree(repo, worktree, work, base)

        state = {
            "schema": "agents-team/state@1", "card": card, "repo_id": rid, "flow": FLOW,
            "phase": "created", "iteration": 0, "plan_round": 0,
            "max_iterations": self.team.get("max_iterations", 3),
            "worktree": str(worktree), "branches": {"base": base, "work": work, "official": official},
            "verdicts": {}, "pending_jira_actions": [], "dispute_iterations": [],
            "created_at": now(), "updated_at": now(),
            "history": [{"at": now(), "from": None, "to": "created", "by": "orchestrator"}],
        }
        store.save(state)
        return state

    def run(self, repo_id: str, card: str, max_steps: int = 40) -> dict[str, Any]:
        store, state = self._open(repo_id, card)
        with self._lock(store.card_dir):
            return self._advance(store, state, max_steps)

    @staticmethod
    @contextmanager
    def _lock(card_dir: Path):
        """One orchestrator per card. A lock left by a dead process is taken over."""
        lock = card_dir / ".run.lock"
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)
            raise EngineError(f"card is already being run by pid {pid}; check `agents-team status`")
        except (FileNotFoundError, ValueError, ProcessLookupError):
            pass
        except PermissionError:
            raise EngineError(f"card is locked by another user's process ({lock})")
        lock.write_text(str(os.getpid()))
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _advance(self, store: CardStore, state: dict[str, Any], max_steps: int) -> dict[str, Any]:
        for _ in range(max_steps):
            if state["phase"] in TERMINAL or state["phase"] == "awaiting_human":
                return state
            handler = getattr(self, f"_phase_{state['phase']}", None)
            if handler is None:
                raise EngineError(f"no handler for phase {state['phase']!r} in flow {FLOW}")
            try:
                state = handler(store, state)
            except Escalate as esc:
                state["escalation"] = {"reason": esc.reason, "detail": esc.detail}
                state = store.transition(state, "escalated", note=esc.detail[:500])
        return state

    def approve(self, repo_id: str, card: str) -> dict[str, Any]:
        store, state = self._open(repo_id, card)
        if state["phase"] != "awaiting_human":
            raise EngineError(f"nothing to approve: card is in phase {state['phase']}")
        what, nxt = state["awaiting"]["what"], state["awaiting"]["next"]
        return store.transition(state, nxt, by="human", note=f"approved {what}")

    def status(self, repo_id: str, card: str) -> dict[str, Any]:
        return self._open(repo_id, card)[1]

    def retry(self, repo_id: str, card: str) -> dict[str, Any]:
        """Human decision: re-run the phase that escalated (after fixing its cause)."""
        store, state = self._open(repo_id, card)
        if state["phase"] != "escalated":
            raise EngineError(f"only escalated cards can be retried; card is in phase {state['phase']}")
        previous = state["history"][-1]["from"]
        if state["escalation"]["reason"] == "max_iterations":
            raise EngineError("max_iterations reached: rework the card or restart it instead of retrying")
        reason = state.pop("escalation")["reason"]
        return store.transition(state, previous, by="human", note=f"retry after {reason}")

    # phases --------------------------------------------------------------------------------

    def _phase_created(self, store: CardStore, state: dict) -> dict:
        return store.transition(state, "plan")

    def _phase_plan(self, store: CardStore, state: dict) -> dict:
        state["plan_round"] = state.get("plan_round", 0) + 1
        card_dir = store.card_dir
        plan = card_dir / "plan.md"
        if plan.exists() and state["plan_round"] > 1:
            shutil.copyfile(plan, card_dir / f"plan.round{state['plan_round'] - 1}.md")
        inputs = self._base_inputs(store, state) | self._existing({"plan-review.json": card_dir / "plan-review.json",
                                                                    "plan-goldfish.json": card_dir / "plan-goldfish.json"})

        def check(_: dict) -> list[str]:
            text = plan.read_text(encoding="utf-8")
            return [f"plan.md is missing section '## {h}'" for h in ("Acceptance criteria mapping", "Public interfaces")
                    if not section(text, h)]

        usage = self._run(store, state, "aang", "plan", inputs, [plan], check=check)
        self._collect_markdown_actions(state, plan, "aang-dev")
        return store.transition(state, "plan_review", by="aang-dev", usage=usage)

    def _phase_plan_review(self, store: CardStore, state: dict) -> dict:
        card_dir = store.card_dir
        out = card_dir / "plan-review.json"
        if out.exists():
            archived = card_dir / f"plan-review.round{state['plan_round'] - 1}.json"
            if state["plan_round"] > 1 and not archived.exists():
                out.rename(archived)  # previous round's review
            else:
                out.unlink()  # leftover from an interrupted run of this same round
        inputs = self._base_inputs(store, state) | {"plan.md": card_dir / "plan.md"}
        usage = self._run(store, state, "sokka", "plan_review", inputs, [out],
                          check=self._report_check(state, "sokka", "plan", 0))
        report = load(out)
        self._collect_report_actions(state, report, "sokka-reviewer")
        state["verdicts"]["plan"] = report["verdict"]
        cfg = self._repo_config(store)
        if report["verdict"] == "approve":
            approval = cfg.get("workflow", {}).get("plan_approval")
            if approval == "goldfish":
                return store.transition(state, "plan_goldfish", by="sokka-reviewer", usage=usage)
            if approval == "human":
                state["awaiting"] = {"what": "plan", "next": "sdet", "detail": f"review {card_dir / 'plan.md'}"}
                return store.transition(state, "awaiting_human", by="sokka-reviewer", usage=usage)
            return store.transition(state, "sdet", by="sokka-reviewer", usage=usage)
        return self._plan_rejected(store, state, usage, out, "sokka-reviewer")

    def _plan_rejected(self, store: CardStore, state: dict, usage: dict, report: Path, by: str) -> dict:
        if state["plan_round"] < self.team.get("max_plan_rounds", 2):
            return store.transition(state, "plan", by=by, usage=usage, note=f"plan sent back to aang by {by}")
        state["awaiting"] = {"what": "plan", "next": "sdet",
                             "detail": f"plan still has blocking findings after {state['plan_round']} rounds; "
                                       f"approve to continue anyway, see {report}"}
        return store.transition(state, "awaiting_human", by=by, usage=usage)

    def _phase_plan_goldfish(self, store: CardStore, state: dict) -> dict:
        """Sokka again, cheap model, no context: can someone who knows nothing but the card follow the plan?"""
        card_dir = store.card_dir
        out = card_dir / "plan-goldfish.json"
        if out.exists():
            archived = card_dir / f"plan-goldfish.round{state['plan_round'] - 1}.json"
            if state["plan_round"] > 1 and not archived.exists():
                out.rename(archived)
            else:
                out.unlink()
        inputs = {"card.md": card_dir / "card.md", "plan.md": card_dir / "plan.md"}  # nothing else, on purpose
        usage = self._run(store, state, "sokka", "plan_goldfish", inputs, [out], isolated=True,
                          check=self._report_check(state, "sokka", "plan", 0, phase="plan_goldfish", path=out))
        report = load(out)
        self._collect_report_actions(state, report, "sokka-reviewer")
        state["verdicts"]["plan_goldfish"] = report["verdict"]
        if report["verdict"] == "approve":
            return store.transition(state, "sdet", by="sokka-reviewer", usage=usage, note="goldfish approved the plan")
        return self._plan_rejected(store, state, usage, out, "sokka-reviewer (goldfish)")

    def _phase_sdet(self, store: CardStore, state: dict) -> dict:
        card_dir = store.card_dir
        state["iteration"] = max(state["iteration"], 1)
        n = state["iteration"]
        test_plan = card_dir / "test-plan.md"
        phase = "fix_tests" if test_plan.exists() else "write_tests"
        inputs = self._base_inputs(store, state) | {"plan.md": card_dir / "plan.md"}
        if phase == "fix_tests":
            inputs |= {"test-plan.md": test_plan}
            inputs |= self._existing({"change-summary.md": iteration_dir(card_dir, n) / "change-summary.md"})
            inputs |= self._previous_reports(card_dir, n)
        usage = self._run(store, state, "toph", phase, inputs, [test_plan])
        self._collect_markdown_actions(state, test_plan, "toph-sdet")
        return store.transition(state, "dev", by="toph-sdet", usage=usage, note=phase)

    def _phase_dev(self, store: CardStore, state: dict) -> dict:
        card_dir, n = store.card_dir, state["iteration"]
        summary = iteration_dir(card_dir, n) / "change-summary.md"
        inputs = self._base_inputs(store, state) | {"plan.md": card_dir / "plan.md", "test-plan.md": card_dir / "test-plan.md"}
        inputs |= self._previous_reports(card_dir, n)
        usage = self._run(store, state, "aang", "implement", inputs, [summary])
        self._collect_markdown_actions(state, summary, "aang-dev")
        disputed = has_content(section(summary.read_text(encoding="utf-8"), "Disputed tests"))
        if disputed and n not in state["dispute_iterations"]:
            state["dispute_iterations"].append(n)
            return store.transition(state, "sdet", by="aang-dev", usage=usage, note="aang disputed tests")
        return store.transition(state, "review", by="aang-dev", usage=usage)

    def _phase_review(self, store: CardStore, state: dict) -> dict:
        card_dir, n = store.card_dir, state["iteration"]
        it = iteration_dir(card_dir, n)
        out = it / "review.json"
        inputs = self._base_inputs(store, state) | {
            "plan.md": card_dir / "plan.md", "test-plan.md": card_dir / "test-plan.md",
            "change-summary.md": it / "change-summary.md"}
        inputs |= self._existing({"previous review.json": iteration_dir(card_dir, n - 1) / "review.json"})
        usage = self._run(store, state, "sokka", "review", inputs, [out], check=self._report_check(state, "sokka", "code", n))
        report = load(out)
        self._collect_report_actions(state, report, "sokka-reviewer")
        state["verdicts"].setdefault(f"{n:02d}", {})["reviewer"] = report["verdict"]
        return store.transition(state, "qa_sec", by="sokka-reviewer", usage=usage)

    def _phase_qa_sec(self, store: CardStore, state: dict) -> dict:
        card_dir, n = store.card_dir, state["iteration"]
        it, prev = iteration_dir(card_dir, n), iteration_dir(card_dir, n - 1)
        base = self._base_inputs(store, state) | {"test-plan.md": card_dir / "test-plan.md",
                                                  "change-summary.md": it / "change-summary.md"}
        jobs = {
            "katara": ("qa", base | self._existing({"previous qa.json": prev / "qa.json"}), it / "qa.json"),
            "zuko": ("sec", base | {"plan.md": card_dir / "plan.md"} | self._existing({"previous sec.json": prev / "sec.json"}),
                     it / "sec.json"),
        }
        # Snapshot once for both: each run may only add its own outputs.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {name: pool.submit(self._run, store, state, name, phase, inputs, [out],
                                         check=self._report_check(state, name, "code", n),
                                         scratch=[it / "raw" / name], concurrent_outputs=[o for _, (_, _, o) in jobs.items()]
                                         + [it / "raw"])
                       for name, (phase, inputs, out) in jobs.items()}
            usages = {name: f.result() for name, f in futures.items()}
        for name, role_key, author in (("katara", "qa", "katara-qa"), ("zuko", "sec", "zuko-sec")):
            report = load(jobs[name][2])
            self._collect_report_actions(state, report, author)
            state["verdicts"].setdefault(f"{n:02d}", {})[role_key] = report["verdict"]
        usage = {"model": "+".join(u.get("model", "?") for u in usages.values()),
                 "credits": round(sum(u.get("credits", 0) for u in usages.values()), 4),
                 "seconds": max((u.get("seconds", 0) for u in usages.values()), default=0)}
        return store.transition(state, "consolidate", usage=usage)

    def _phase_consolidate(self, store: CardStore, state: dict) -> dict:
        card_dir, n = store.card_dir, state["iteration"]
        it = iteration_dir(card_dir, n)
        verdicts = state["verdicts"].get(f"{n:02d}", {})
        if all(verdicts.get(k) == "approve" for k in ("reviewer", "qa", "sec")):
            return store.transition(state, "grouping_plan", note="all approved")
        if n >= state["max_iterations"]:
            raise Escalate("max_iterations", f"iteration {n} still has {verdicts}; see {it}")
        open_findings = []
        for name, author in REPORT_AUTHORS.items():
            for finding in load(it / name)["findings"]:
                if finding.get("status") not in {"fixed", "wont_fix"}:
                    open_findings.append((finding, author))
        worktree = Path(state["worktree"])
        placements = notes.insert(worktree, open_findings)
        write_json(it / "notes.json", [p.__dict__ for p in placements])
        gitops.commit_files(worktree, sorted({p.file for p in placements if p.placed}),
                            f"orchestrator: add review notes (iteration {n:02d})", as_orchestrator=True)
        state["iteration"] = n + 1
        toph_owned = any(f.get("blocking") and f["owner"] == "toph-sdet" for f, _ in open_findings)
        return store.transition(state, "sdet" if toph_owned else "dev", note=f"verdicts {verdicts}")

    def _phase_grouping_plan(self, store: CardStore, state: dict) -> dict:
        worktree, branches = Path(state["worktree"]), state["branches"]
        card_md = (store.card_dir / "card.md").read_text(encoding="utf-8")
        cfg = self._repo_config(store)
        title = card_title(card_md, state["card"])
        commits_cfg = cfg.get("commits", {})
        convention = commits_cfg.get("convention", "conventional")
        subject = {"conventional": f"feat: {title}", "ticket-dash": f"{state['card']} - {title}"}.get(convention, title)
        authors = sorted({s.split(":", 1)[0] for _, s in gitops.commits(worktree, branches["base"], branches["work"])
                          if s.split(":", 1)[0] in {"aang-dev", "toph-sdet"}})
        message = f"{subject}\n"
        if convention != "ticket-dash":
            message += f"\nRefs: {state['card']}\n"
        if commits_cfg.get("trailers", True):
            message += (f"\nAgent-Authors: {', '.join(authors) or 'none'}\n"
                        f"Agent-Reviewed: sokka-reviewer, katara-qa, zuko-sec\n")
        files = [f for f in gitops.changed_files(worktree, branches["base"], branches["work"])
                 if not globs.match_any(f, JUNK)]
        plan = (f"# Grouping plan: {state['card']}\n\n"
                f"Official branch: `{branches['official']}` from `{branches['base']}`.\n\n"
                "All work-branch commits are squashed into one commit. Edit the message below before "
                "approving; it is used verbatim. AGENT-NOTE lines are removed automatically.\n\n"
                f"## Commit message\n\n```text\n{message}```\n\n"
                "## Files\n\n" + "\n".join(f"- `{f}`" for f in files) + "\n")
        (store.card_dir / "grouping-plan.md").write_text(plan, encoding="utf-8")
        state["awaiting"] = {"what": "grouping", "next": "assemble", "detail": f"edit and approve {store.card_dir / 'grouping-plan.md'}"}
        return store.transition(state, "awaiting_human")

    def _phase_assemble(self, store: CardStore, state: dict) -> dict:
        worktree, branches = Path(state["worktree"]), state["branches"]
        text = (store.card_dir / "grouping-plan.md").read_text(encoding="utf-8")
        match = re.search(r"```text\n(.*?)```", section(text, "Commit message"), re.S)
        if not match or not match.group(1).strip():
            raise Escalate("human_requested", "grouping-plan.md has no commit message block")
        tracked_dirty = gitops.status(worktree)[0]
        if tracked_dirty:
            raise Escalate("agent_error", f"worktree has uncommitted changes: {sorted(tracked_dirty)[:10]}")
        gitops.switch_new(worktree, branches["official"], branches["base"])
        gitops.merge_squash(worktree, branches["work"])
        staged = gitops.git(worktree, "diff", "--cached", "--name-only").splitlines()
        junk = [f for f in staged if globs.match_any(f, JUNK)]
        if junk:  # e.g. bytecode an agent committed in a repo without .gitignore
            gitops.git(worktree, "rm", "-q", "--cached", "--", *junk)
            staged = [f for f in staged if f not in junk]
        removed = notes.strip(worktree, staged)
        present = [f for f in staged if (worktree / f).exists()]
        if present:
            gitops.git(worktree, "add", "--", *present)
        msg_file = store.card_dir / ".commit-message.txt"
        msg_file.write_text(match.group(1), encoding="utf-8")
        gitops.git(worktree, "commit", "-q", "-F", str(msg_file))
        msg_file.unlink()
        return store.transition(state, "final_gates", note=f"official commit {gitops.head(worktree)[:10]}, {removed} notes stripped")

    def _phase_final_gates(self, store: CardStore, state: dict) -> dict:
        worktree, branches = Path(state["worktree"]), state["branches"]
        cfg = self._repo_config(store)
        results: list[dict[str, Any]] = []
        files = gitops.changed_files(worktree, branches["base"], branches["official"])
        leftover = notes.find(worktree, files)
        results.append({"gate": "agent-notes", "ok": not leftover, "detail": leftover[:20]})
        for key in ("test", "bdd", "lint"):
            cmd = cfg["commands"].get(key)
            if not cmd:
                continue
            try:
                proc = subprocess.run(shlex.split(cmd), cwd=worktree, capture_output=True, text=True,
                                      timeout=self.gate_timeout_secs, stdin=subprocess.DEVNULL)
                ok, tail = proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]
            except (OSError, subprocess.TimeoutExpired) as exc:
                ok, tail = False, str(exc)
            results.append({"gate": f"commands.{key}", "command": cmd, "ok": ok, "output_tail": tail})
        write_json(store.card_dir / "final-gates.json", results)
        failed = [r["gate"] for r in results if not r["ok"]]
        self._final_report(store, state, results)
        if failed:
            raise Escalate("gate_failed", f"final gates failed: {failed}; see {store.card_dir / 'final-gates.json'}")
        if state["pending_jira_actions"]:
            state["awaiting"] = {"what": "jira_actions", "next": "jira_sync",
                                 "detail": f"{len(state['pending_jira_actions'])} Jira actions in {store.card_dir / 'final-report.md'}"}
            return store.transition(state, "awaiting_human", note="gates passed")
        return store.transition(state, "done", note="gates passed; push and MR are up to the human")

    def _phase_jira_sync(self, store: CardStore, state: dict) -> dict:
        # Applying actions through the Atlassian MCP is the next increment. Until then the approved
        # batch is written for the human (or a later run) to apply.
        write_json(store.card_dir / "jira-actions.approved.json", state["pending_jira_actions"])
        return store.transition(state, "done", note="jira actions approved and exported; not applied automatically yet")

    # agent runs ----------------------------------------------------------------------------

    def _run(self, store: CardStore, state: dict, name: str, phase: str, inputs: dict[str, Path],
             outputs: list[Path], *, check: Callable[[dict], list[str]] | None = None,
             scratch: list[Path] = (), concurrent_outputs: list[Path] = (), isolated: bool = False) -> dict[str, Any]:
        agent = self.agents[name]
        card_dir, worktree = store.card_dir, Path(state["worktree"])
        prefix = f"{agent.name}-{agent.meta['role']}"
        allowed_out = {p.resolve() for p in outputs}
        scratch_dirs = [p.resolve() for p in scratch]
        others = [p.resolve() for p in concurrent_outputs]
        logs = (card_dir / "logs").resolve()
        # Isolated runs start in an empty directory (so the harness injects no repo README/AGENTS.md)
        # and may read only their declared inputs.
        sandbox = (card_dir / ".isolated" / f"{name}-{phase}").resolve() if isolated else None

        def skip(path: Path) -> bool:
            p = path.resolve()
            return (p in allowed_out or p in others or logs in p.parents
                    or (sandbox is not None and (p == sandbox or sandbox in p.parents))
                    or any(d == p or d in p.parents for d in scratch_dirs + others))

        cfg = self._repo_config(store)
        deny_read: tuple[str, ...] = ()
        if sandbox is not None:
            shutil.rmtree(sandbox, ignore_errors=True)
            sandbox.mkdir(parents=True)
            readable = {p.resolve() for p in inputs.values()}
            deny_read = (f"{worktree}/**", f"{self.layout.repo_dir(state['repo_id'])}/memory/**",
                         *[str(p) for p in sorted(card_dir.resolve().rglob("*"))
                           if p.is_file() and p not in readable and p not in allowed_out and not skip(p)])
        ctx = RunContext(worktree=sandbox or worktree, card_dir=card_dir, repo_config=cfg,
                         team_models=self.team.get("models", {}),
                         outputs=tuple([str(p) for p in allowed_out] + [f"{d}/**" for d in scratch_dirs]),
                         deny_read=deny_read, allow_shell=not isolated)
        before_head, before_digest = gitops.head(worktree), _digest(card_dir, skip)
        untracked_before = gitops.status(worktree)[1]
        errors_seen: list[str] = []
        usage_total: dict[str, Any] = {"model": agent.model_for(phase, ctx.team_models), "credits": 0.0, "seconds": 0.0}
        for attempt in (1, 2):
            for p in allowed_out:
                p.parent.mkdir(parents=True, exist_ok=True)
            for d in scratch_dirs:
                d.mkdir(parents=True, exist_ok=True)
            task = self._task(state, agent, phase, inputs, sorted(allowed_out), scratch_dirs, cfg, errors_seen,
                              isolated=isolated)
            log = card_dir / "logs" / f"it{state['iteration']:02d}-{name}-{phase}-{attempt}.log"
            result = self.runner.run(RunRequest(agent, phase, task, ctx, log))
            usage_total["credits"] = round(usage_total["credits"] + result.usage.get("credits", 0), 4)
            usage_total["seconds"] += result.usage.get("seconds", 0)
            self._check_integrity(card_dir, before_digest, skip, name)
            self._check_repo(worktree, before_head, untracked_before, agent, phase, cfg, prefix)
            if not result.ok:
                raise Escalate("agent_error", f"{name}/{phase} exited with {result.exit_code}; log {log}")
            errors_seen = self._check_outputs(allowed_out, check)
            if not errors_seen:
                return usage_total
        raise Escalate("agent_error", f"{name}/{phase} outputs still invalid after retry: {errors_seen[:5]}")

    def _check_integrity(self, card_dir: Path, before: dict[str, str], skip: Callable[[Path], bool], name: str) -> None:
        after = _digest(card_dir, skip)
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        if changed:
            raise Escalate("agent_error", f"{name} changed artifacts it does not own: {changed[:10]}")

    def _check_repo(self, worktree: Path, before: str, untracked_before: set[str], agent: AgentDefinition,
                    phase: str, cfg: dict[str, Any], prefix: str) -> None:
        """Committed and tracked changes must be in scope. New untracked files inside the scope are
        the agent's work and get committed; untracked files outside it (caches, reports) are ignored."""
        perms, paths = agent.meta["permissions"], cfg.get("paths", {})
        allowed = [g for s in perms["write"].get(phase, []) if s.startswith("paths.") for g in paths.get(s[6:], [])]
        deny_scopes = perms.get("deny_write", [])
        denied = [g for s in deny_scopes if s.startswith("paths.") for g in paths.get(s[6:], [])]
        if "repo" in deny_scopes:
            denied.append("**")

        def in_scope(f: str) -> bool:
            return globs.match_any(f, allowed) and not globs.match_any(f, denied)

        tracked, untracked = gitops.status(worktree)
        new_untracked = untracked - untracked_before
        committed = set(gitops.changed_files(worktree, before))
        bad = sorted(f for f in committed | tracked if not in_scope(f) and not globs.match_any(f, JUNK))
        if bad:
            raise Escalate("agent_error", f"{prefix} changed files outside its write scope: {bad[:10]}")
        for sha, subject in gitops.commits(worktree, before):
            if not subject.startswith(f"{prefix}:"):
                raise Escalate("agent_error", f"commit {sha[:10]} by {prefix} has wrong prefix: {subject!r}")
        pending = sorted(tracked | {f for f in new_untracked if in_scope(f) and not globs.match_any(f, JUNK)})
        gitops.commit_files(worktree, pending, f"{prefix}: commit pending changes (left uncommitted by agent)")

    @staticmethod
    def _check_outputs(outputs: set[Path], check: Callable[[dict], list[str]] | None) -> list[str]:
        missing = [str(p) for p in outputs if not p.is_file() or not p.read_text(encoding="utf-8").strip()]
        if missing:
            return [f"missing or empty output: {m}" for m in missing]
        return check({}) if check else []

    def _report_check(self, state: dict, name: str, stage: str, iteration: int, *, phase: str | None = None,
                      path: Path | None = None) -> Callable[[dict], list[str]]:
        agent = self.agents[name]
        role = agent.meta["role"]
        out_name = {"plan": "plan-review.json"}.get(stage) or {"reviewer": "review.json", "qa": "qa.json", "sec": "sec.json"}[role]
        run_phase = phase or self._phase_for(name, stage)

        def check(_: dict) -> list[str]:
            card_dir = self.layout.card_dir(state["repo_id"], state["card"])
            report_path = path or (card_dir / out_name if stage == "plan" else iteration_dir(card_dir, iteration) / out_name)
            try:
                report = load(report_path)
            except (json.JSONDecodeError, OSError) as exc:
                return [f"{report_path.name} is not valid JSON: {exc}"]
            problems = errors("finding-report", report) if isinstance(report, dict) else ["report must be a JSON object"]
            if problems:  # schema errors first; the extra checks assume a well-formed report
                return problems
            expected = {"card": state["card"], "flow": state["flow"], "stage": stage, "iteration": iteration}
            problems += [f"{k} must be {v!r}, got {report.get(k)!r}" for k, v in expected.items() if report.get(k) != v]
            who = report["agent"]
            if who["name"] != agent.name or who["role"] != role:
                problems.append(f"agent must be name={agent.name!r} role={role!r}")
            if not problems:  # record the model that actually ran, not the one the agent claims
                report["agent"]["model"] = agent.model_for(run_phase, self.team.get("models", {}))
                write_json(report_path, report)
            return problems
        return check

    @staticmethod
    def _phase_for(name: str, stage: str) -> str:
        return {"sokka": "plan_review" if stage == "plan" else "review", "katara": "qa", "zuko": "sec"}[name]

    def _task(self, state: dict, agent: AgentDefinition, phase: str, inputs: dict[str, Path], outputs: list[Path],
              scratch: list[Path], cfg: dict, previous_errors: list[str], isolated: bool = False) -> str:
        perms = agent.meta["permissions"]
        paths = cfg.get("paths", {})
        write = [g for s in perms["write"].get(phase, []) if s.startswith("paths.") for g in paths.get(s[6:], [])]
        commands = []
        for item in perms["shell"]:
            if item.startswith("commands.") and cfg["commands"].get(item[9:]):
                commands.append(cfg["commands"][item[9:]])
            elif item == "security.tools":
                commands += cfg.get("security", {}).get("tools", [])
            elif item.startswith("git:"):
                commands.append(f"git {item[4:]}")
        worktree = Path(state["worktree"])
        head = [
            "# agents-team task", "",
            f"- card: {state['card']}", f"- flow: {state['flow']}", f"- phase: {phase}",
            f"- iteration: {state['iteration']}",
        ]
        if isolated:
            head += ["- isolated run: you have NO repository, NO shell and NO other artifacts. "
                     "Read only the inputs below; every other read is denied.", ""]
        else:
            head += [f"- worktree (your working directory): {worktree}",
                     f"- base branch: {state['branches']['base']}; work branch: {state['branches']['work']}",
                     f"- current commit: {gitops.head(worktree)}", ""]
        lines = [
            *head,
            "## Inputs (read these first)", *[f"- {k}: {v}" for k, v in inputs.items()], "",
            "## Outputs (write exactly these files; you may not write any other artifact)",
            *[f"- {p}" for p in outputs], *[f"- scratch directory for raw tool output: {d}" for d in scratch],
        ]
        if not isolated:
            lines += ["", "## Repository write scope",
                      *([f"- {g}" for g in write] or ["- none: do not change repository files"]), "",
                      "## Allowed shell commands (one per call, extra safe arguments allowed)",
                      *[f"- `{c}`" for c in commands]]
        if agent.name == "zuko":
            lines += ["", f"## Security threshold\n- block_severity: {cfg.get('security', {}).get('block_severity', 'high')}"]
        json_outputs = [p for p in outputs if p.suffix == ".json"]
        if json_outputs:
            stage = "plan" if phase in {"plan_review", "plan_goldfish"} else "code"
            header = {"schema": "agents-team/finding-report@1", "card": state["card"], "flow": state["flow"],
                      "stage": stage, "iteration": 0 if stage == "plan" else state["iteration"],
                      "agent": {"name": agent.name, "role": agent.meta["role"],
                                "model": agent.model_for(phase, self.team.get("models", {}))},
                      "verdict": "approve | request_changes | block", "summary": "...", "findings": [],
                      "jira_actions": []}
            if agent.meta["role"] == "qa":
                header["test_run"] = {"command": cfg["commands"].get("test", "..."), "exit_code": 0,
                                      "passed": 0, "failed": 0, "skipped": 0, "flaky": []}
            if agent.meta["role"] == "sec":
                header["tools_run"] = [{"name": t, "status": "ran | not_installed | failed"}
                                       for t in cfg.get("security", {}).get("tools", [])]
            finding = {"id": f"{ {'reviewer': 'R', 'qa': 'Q', 'sec': 'S'}.get(agent.meta['role'], 'T') }-001",
                       "severity": "info | low | medium | high | critical", "category": "correctness",
                       "file": "path/relative/to/repo.py", "line": 1, "message": "...", "suggestion": "...",
                       "blocking": True, "owner": "aang-dev | toph-sdet | human"}
            lines += ["", "## JSON report header (use these exact values)", "```json",
                      json.dumps(header, indent=2, ensure_ascii=False), "```",
                      "Each entry of `findings` looks like this (`file` and `line` are optional; "
                      "`test_run` and `tools_run` are required when shown above):", "```json",
                      json.dumps(finding, indent=2, ensure_ascii=False), "```",
                      "Schema: schemas/finding-report.schema.json in the agents-team repository. "
                      "Unknown keys are rejected."]
        if previous_errors:
            lines += ["", "## Your previous attempt was rejected. Fix these problems:", *[f"- {e}" for e in previous_errors]]
        return "\n".join(lines) + "\n"

    # misc ----------------------------------------------------------------------------------

    def _open(self, repo_id: str, card: str) -> tuple[CardStore, dict]:
        store = CardStore(self.layout.card_dir(repo_id, card))
        if not store.exists():
            raise EngineError(f"card {card} not found for repo {repo_id}")
        return store, store.load()

    def _repo_config(self, store: CardStore) -> dict[str, Any]:
        return load(store.card_dir / "agents-team.yml")

    def _base_inputs(self, store: CardStore, state: dict) -> dict[str, Path]:
        mem = self.layout.memory_dir(state["repo_id"])
        return {"card.md": store.card_dir / "card.md"} | self._existing(
            {"conventions.md": mem / "conventions.md", "lessons.md": mem / "lessons.md"})

    @staticmethod
    def _existing(candidates: dict[str, Path]) -> dict[str, Path]:
        return {k: v for k, v in candidates.items() if v.is_file()}

    def _previous_reports(self, card_dir: Path, n: int) -> dict[str, Path]:
        prev = iteration_dir(card_dir, n - 1)
        return self._existing({f"previous {k}": prev / k for k in ("review.json", "qa.json", "sec.json", "notes.json")})

    def _add_actions(self, state: dict, actions: list[Any], author: str) -> None:
        for action in actions:
            if list(_JIRA_ACTION.iter_errors(action)):
                continue
            entry = {"from": author, "action": action}
            if entry not in state["pending_jira_actions"]:
                state["pending_jira_actions"].append(entry)

    def _collect_report_actions(self, state: dict, report: dict, author: str) -> None:
        self._add_actions(state, report.get("jira_actions", []), author)

    def _collect_markdown_actions(self, state: dict, path: Path, author: str) -> None:
        self._add_actions(state, markdown_jira_actions(path.read_text(encoding="utf-8")), author)

    def _final_report(self, store: CardStore, state: dict, gates: list[dict]) -> None:
        b = state["branches"]
        lines = [f"# Final report: {state['card']}", "",
                 f"- Official branch: `{b['official']}` (base `{b['base']}`); work branch `{b['work']}` stays local.",
                 f"- Iterations: {state['iteration']}; plan rounds: {state.get('plan_round', 0)}",
                 f"- Credits used: {state.get('usage_total', 0)}", "", "## Gates",
                 *[f"- {'PASS' if g['ok'] else 'FAIL'} {g['gate']}" for g in gates], "", "## Verdicts",
                 *[f"- {k}: {v}" for k, v in state["verdicts"].items()], "", "## Jira actions (need your approval)"]
        lines += [f"- [{a['from']}] `{json.dumps(a['action'], ensure_ascii=False)}`" for a in state["pending_jira_actions"]] or ["- none"]
        lines += ["", "## Next steps (human)", f"1. Review `git log {b['base']}..{b['official']}` in {state['worktree']}.",
                  f"2. Push `{b['official']}` and open the MR yourself."]
        (store.card_dir / "final-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
