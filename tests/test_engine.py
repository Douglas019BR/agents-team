"""End-to-end tests of the implement-system flow with a scripted fake runner (no LLM calls)."""

import json
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from agents_team import gitops, notes
from agents_team.engine import Engine, EngineError, repo_id_for
from agents_team.runner import RunRequest, RunResult
from agents_team.store import Layout

CARD = "QAPC-42"
CARD_MD = "# Validar token expirado\n\n## Critérios de aceite\n1. Token expirado lança InvalidToken.\n"


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(worktree: Path, prefix: str, msg: str) -> None:
    sh(worktree, "add", "-A")
    sh(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"{prefix}: {msg}")


# --- fake agents -------------------------------------------------------------------------------

def out(req: RunRequest, suffix: str) -> Path:
    return next(Path(o) for o in req.ctx.outputs if o.endswith(suffix))


def report(req: RunRequest, verdict="approve", findings=(), stage="code", jira=()):
    task = req.task
    iteration = int(task.split("- iteration: ")[1].split("\n")[0])
    name = req.agent.name
    path = out(req, ".json")
    data = {"schema": "agents-team/finding-report@1", "card": CARD, "flow": "implement-system", "stage": stage,
            "iteration": 0 if stage == "plan" else iteration,
            "agent": {"name": name, "role": req.agent.meta["role"], "model": "claims-anything"},
            "verdict": verdict, "summary": "ok", "findings": list(findings), "jira_actions": list(jira)}
    path.write_text(json.dumps(data))


def plan(req):
    out(req, "plan.md").write_text("# Plan\n## Acceptance criteria mapping\n| a | b |\n## Public interfaces\n`check(token)`\n")


def write_tests(req):
    wt = req.ctx.worktree
    (wt / "tests" / "test_token.py").write_text("def test_expired():\n    assert True\n")
    out(req, "test-plan.md").write_text("# Test plan\n## Cases\n| TC-01 |\n")
    commit(wt, "toph-sdet", "add expired token test")


def implement(req):
    wt = req.ctx.worktree
    path = wt / "src" / "token.py"
    lines = [l for l in path.read_text().splitlines() if "AGENT-NOTE" not in l] if path.exists() else []
    lines.append(f"VERSION = {len(lines)}")
    path.write_text("\n".join(lines) + "\n")
    out(req, "change-summary.md").write_text("# Summary\n## What changed\n- token\n## Disputed tests\nNone\n")
    commit(wt, "aang-dev", "implement token check")


def approve(req):
    report(req)


BLOCKING = {"id": "R-001", "severity": "high", "category": "correctness", "file": "src/token.py", "line": 1,
            "message": "handle None", "blocking": True, "owner": "aang-dev"}


class FakeRunner:
    def __init__(self, overrides=None):
        self.behaviors = {
            ("aang", "plan"): plan,
            ("sokka", "plan_review"): lambda r: report(r, stage="plan"),
            ("toph", "write_tests"): write_tests,
            ("toph", "fix_tests"): lambda r: out(r, "test-plan.md").write_text("# Test plan v2\n"),
            ("aang", "implement"): implement,
            ("sokka", "review"): approve,
            ("katara", "qa"): approve,
            ("zuko", "sec"): approve,
        } | (overrides or {})
        self.calls = defaultdict(int)

    def run(self, req: RunRequest) -> RunResult:
        key = (req.agent.name, req.phase)
        self.calls[key] += 1
        behavior = self.behaviors[key]
        behavior(req) if behavior.__code__.co_argcount == 1 else behavior(req, self.calls[key])
        return RunResult(ok=True, output="", usage={"credits": 0.5, "seconds": 1.0})


# --- fixtures ----------------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "target"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "token.py").write_text("VERSION = 0\n")
    (root / "tests" / "test_existing.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    sh(root, "remote", "add", "origin", "git@gitlab.example.com:grp/target.git")
    commit(root, "chore", "init")
    cfg = {"version": 1, "stack": {"language": "python"},
           "commands": {"test": "python3 -m compileall -q src tests"},
           "paths": {"source": ["src/**"], "tests": ["tests/**"], "protected": [".env*"]},
           "commits": {"convention": "conventional"}}
    (root / ".agents-team.yml").write_text(yaml.safe_dump(cfg))
    return root


def make_engine(tmp_path, runner, **team):
    return Engine(layout=Layout(tmp_path / "home"), runner=runner, team=team)


def started(tmp_path, repo, runner, **team):
    engine = make_engine(tmp_path, runner, **team)
    engine.start(repo, CARD, CARD_MD)
    return engine, repo_id_for(repo)


# --- tests -------------------------------------------------------------------------------------

def test_start_requires_discovery(tmp_path, repo):
    (repo / ".agents-team.yml").unlink()
    with pytest.raises(EngineError, match="discovery"):
        make_engine(tmp_path, FakeRunner()).start(repo, CARD, CARD_MD)


def test_start_creates_worktree_branch_and_state(tmp_path, repo):
    engine, rid = started(tmp_path, repo, FakeRunner())
    state = engine.status(rid, CARD)
    assert rid == "grp-target"
    assert state["branches"]["official"] == "feature/QAPC-42-validar-token-expirado"
    assert sh(Path(state["worktree"]), "branch", "--show-current") == "agents/QAPC-42/work"
    assert sh(repo, "status", "--porcelain") == ""  # .agents-team.yml excluded, repo untouched
    with pytest.raises(EngineError, match="already started"):
        engine.start(repo, CARD, CARD_MD)


def test_full_flow_with_one_fix_iteration(tmp_path, repo):
    def review(req, n):
        if n == 1:
            report(req, "request_changes", [BLOCKING], jira=[{"type": "comment", "body": "CA ambíguo"}])
        else:
            report(req)

    runner = FakeRunner({("sokka", "review"): review})
    engine, rid = started(tmp_path, repo, runner)
    state = engine.run(rid, CARD)
    assert state["phase"] == "awaiting_human" and state["awaiting"]["what"] == "grouping"
    assert state["iteration"] == 2
    assert state["verdicts"]["01"]["reviewer"] == "request_changes"
    assert state["verdicts"]["02"] == {"reviewer": "approve", "qa": "approve", "sec": "approve"}

    wt = Path(state["worktree"])
    work_log = sh(wt, "log", "--format=%s", "main..agents/QAPC-42/work")
    assert "orchestrator: add review notes (iteration 01)" in work_log
    card_dir = Path(tmp_path / "home" / "repos" / rid / "cards" / CARD)
    assert json.loads((card_dir / "iterations" / "01" / "notes.json").read_text())[0]["placed"] is True
    # the orchestrator records the real model, not the agent's claim
    assert json.loads((card_dir / "iterations" / "02" / "review.json").read_text())["agent"]["model"] == "gpt-5.6-terra"

    state = engine.approve(rid, CARD)
    state = engine.run(rid, CARD)
    assert state["phase"] == "awaiting_human" and state["awaiting"]["what"] == "jira_actions"
    state = engine.approve(rid, CARD)
    state = engine.run(rid, CARD)
    assert state["phase"] == "done"

    official = state["branches"]["official"]
    assert sh(wt, "rev-list", "--count", f"main..{official}") == "1"
    message = sh(wt, "log", "-1", "--format=%B", official)
    assert message.startswith("feat: Validar token expirado")
    assert "Agent-Authors: aang-dev, toph-sdet" in message
    assert not notes.find(wt, gitops.changed_files(wt, "main", official))
    assert json.loads((card_dir / "jira-actions.approved.json").read_text())[0]["action"]["body"] == "CA ambíguo"
    assert state["usage_total"] > 0
    assert (card_dir / "final-report.md").exists()
    assert "main" in sh(repo, "branch", "--list", "main")  # base untouched
    assert sh(repo, "rev-list", "--count", "main") == "1"


def test_dev_touching_tests_is_escalated(tmp_path, repo):
    def cheat(req):
        implement(req)
        (req.ctx.worktree / "tests" / "test_token.py").write_text("def test_expired():\n    pass\n")
        commit(req.ctx.worktree, "aang-dev", "relax test")

    engine, rid = started(tmp_path, repo, FakeRunner({("aang", "implement"): cheat}))
    state = engine.run(rid, CARD)
    assert state["phase"] == "escalated"
    assert "outside its write scope" in state["escalation"]["detail"]
    assert "tests/test_token.py" in state["escalation"]["detail"]


def test_reviewer_editing_repo_is_escalated(tmp_path, repo):
    def sneaky(req):
        (req.ctx.worktree / "src" / "token.py").write_text("VERSION = 99\n")
        report(req)

    engine, rid = started(tmp_path, repo, FakeRunner({("sokka", "review"): sneaky}))
    state = engine.run(rid, CARD)
    assert state["phase"] == "escalated" and "sokka-reviewer" in state["escalation"]["detail"]


def test_agent_forging_another_report_is_escalated(tmp_path, repo):
    def forge(req):
        implement(req)
        plan_review = req.ctx.card_dir / "plan-review.json"
        data = json.loads(plan_review.read_text())
        data["summary"] = "forged"
        plan_review.write_text(json.dumps(data))

    engine, rid = started(tmp_path, repo, FakeRunner({("aang", "implement"): forge}))
    state = engine.run(rid, CARD)
    assert state["phase"] == "escalated"
    assert "does not own" in state["escalation"]["detail"] and "plan-review.json" in state["escalation"]["detail"]


def test_invalid_report_gets_one_retry(tmp_path, repo):
    def flaky(req, n):
        if n == 1:
            out(req, ".json").write_text('{"verdict": "approve"}')
        else:
            assert "previous attempt was rejected" in req.task
            report(req)

    runner = FakeRunner({("katara", "qa"): flaky})
    engine, rid = started(tmp_path, repo, runner)
    assert engine.run(rid, CARD)["awaiting"]["what"] == "grouping"
    assert runner.calls[("katara", "qa")] == 2


def test_invalid_report_twice_escalates(tmp_path, repo):
    runner = FakeRunner({("zuko", "sec"): lambda r: out(r, ".json").write_text("not json")})
    engine, rid = started(tmp_path, repo, runner)
    state = engine.run(rid, CARD)
    assert state["phase"] == "escalated" and "after retry" in state["escalation"]["detail"]


def test_max_iterations_escalates(tmp_path, repo):
    runner = FakeRunner({("sokka", "review"): lambda r: report(r, "request_changes", [BLOCKING])})
    engine, rid = started(tmp_path, repo, runner)
    state = engine.run(rid, CARD)
    assert state["phase"] == "escalated"
    assert state["escalation"]["reason"] == "max_iterations"
    assert runner.calls[("aang", "implement")] == 3


def test_plan_sent_back_once(tmp_path, repo):
    def plan_review(req, n):
        finding = {k: v for k, v in BLOCKING.items() if k not in {"file", "line"}}
        report(req, "request_changes" if n == 1 else "approve", [finding] if n == 1 else [], stage="plan")

    runner = FakeRunner({("sokka", "plan_review"): lambda r, n: plan_review(r, n)})
    engine, rid = started(tmp_path, repo, runner)
    state = engine.run(rid, CARD)
    assert runner.calls[("aang", "plan")] == 2
    assert state["plan_round"] == 2
    card_dir = tmp_path / "home" / "repos" / rid / "cards" / CARD
    assert (card_dir / "plan.round1.md").exists() and (card_dir / "plan-review.round1.json").exists()


def test_plan_approval_by_human(tmp_path, repo):
    cfg = yaml.safe_load((repo / ".agents-team.yml").read_text())
    cfg["workflow"] = {"plan_approval": "human"}
    (repo / ".agents-team.yml").write_text(yaml.safe_dump(cfg))
    runner = FakeRunner()
    engine, rid = started(tmp_path, repo, runner)
    state = engine.run(rid, CARD)
    assert state["awaiting"] == {"what": "plan", "next": "sdet", "detail": state["awaiting"]["detail"]}
    assert runner.calls[("toph", "write_tests")] == 0
    engine.approve(rid, CARD)
    assert engine.run(rid, CARD)["awaiting"]["what"] == "grouping"


def test_disputed_tests_go_back_to_toph_once(tmp_path, repo):
    def disputing(req, n):
        implement(req)
        out(req, "change-summary.md").write_text("## Disputed tests\n- TC-01 contradicts the card\n")

    runner = FakeRunner({("aang", "implement"): disputing})
    engine, rid = started(tmp_path, repo, runner)
    state = engine.run(rid, CARD)
    assert runner.calls[("toph", "fix_tests")] == 1
    assert runner.calls[("aang", "implement")] == 2  # disputes handled once per iteration
    assert state["dispute_iterations"] == [1]


def test_failed_gate_escalates(tmp_path, repo):
    def broken(req):
        implement(req)
        (req.ctx.worktree / "src" / "broken.py").write_text("def (:\n")
        commit(req.ctx.worktree, "aang-dev", "broken")

    engine, rid = started(tmp_path, repo, FakeRunner({("aang", "implement"): broken}))
    engine.run(rid, CARD)
    engine.approve(rid, CARD)
    state = engine.run(rid, CARD)
    assert state["phase"] == "escalated" and state["escalation"]["reason"] == "gate_failed"


def test_approve_without_gate_fails(tmp_path, repo):
    engine, rid = started(tmp_path, repo, FakeRunner())
    with pytest.raises(EngineError, match="nothing to approve"):
        engine.approve(rid, CARD)


def test_malformed_agent_field_is_retried_not_crashed(tmp_path, repo):
    def bad_then_good(req, n):
        report(req, stage="plan")
        if n == 1:
            path = out(req, ".json")
            data = json.loads(path.read_text())
            data["agent"] = "sokka"
            path.write_text(json.dumps(data))

    runner = FakeRunner({("sokka", "plan_review"): bad_then_good})
    engine, rid = started(tmp_path, repo, runner)
    assert engine.run(rid, CARD)["awaiting"]["what"] == "grouping"
    assert runner.calls[("sokka", "plan_review")] == 2


def test_resume_after_crash_reruns_phase(tmp_path, repo):
    class Boom(Exception):
        pass

    def crash_once(req, n):
        report(req, stage="plan")
        if n == 1:
            raise Boom()

    runner = FakeRunner({("sokka", "plan_review"): crash_once})
    engine, rid = started(tmp_path, repo, runner)
    with pytest.raises(Boom):
        engine.run(rid, CARD)
    assert engine.status(rid, CARD)["phase"] == "plan_review"
    assert engine.run(rid, CARD)["awaiting"]["what"] == "grouping"
    card_dir = tmp_path / "home" / "repos" / rid / "cards" / CARD
    assert not list(card_dir.glob("plan-review.round*.json"))


@pytest.mark.parametrize(
    ("body", "disputed"),
    [
        ("Nenhum teste contestado.", False),
        ("None", False),
        ("- None", False),
        ("- nenhum.", False),
        ("", False),
        ("- TC-03: contradicts the card", True),
        ("1. test_expired: impossible", True),
    ],
)
def test_dispute_detection(body, disputed):
    from agents_team.engine import has_content
    assert has_content(body) is disputed


def test_retry_reruns_escalated_phase(tmp_path, repo):
    def flaky(req, n):
        out(req, ".json").write_text("not json" if n <= 2 else "")
        if n > 2:
            report(req)

    runner = FakeRunner({("zuko", "sec"): flaky})
    engine, rid = started(tmp_path, repo, runner)
    assert engine.run(rid, CARD)["phase"] == "escalated"
    state = engine.retry(rid, CARD)
    assert state["phase"] == "qa_sec" and "escalation" not in state
    assert engine.run(rid, CARD)["awaiting"]["what"] == "grouping"
    with pytest.raises(EngineError, match="only escalated"):
        engine.retry(rid, CARD)


def test_retry_refuses_max_iterations(tmp_path, repo):
    runner = FakeRunner({("sokka", "review"): lambda r: report(r, "request_changes", [BLOCKING])})
    engine, rid = started(tmp_path, repo, runner)
    engine.run(rid, CARD)
    with pytest.raises(EngineError, match="max_iterations"):
        engine.retry(rid, CARD)


def test_junk_never_reaches_official_branch(tmp_path, repo):
    def leaves_junk(req):
        wt = req.ctx.worktree
        (wt / "tests" / "test_token.py").write_text("def test_expired():\n    assert True\n")
        (wt / "tests" / "__pycache__").mkdir()
        (wt / "tests" / "__pycache__" / "test_token.cpython-312.pyc").write_bytes(b"\x00")
        out(req, "test-plan.md").write_text("# Test plan\n")  # no commit: orchestrator commits for it

    def commits_junk(req):
        implement(req)
        (req.ctx.worktree / "src" / "__pycache__").mkdir()
        (req.ctx.worktree / "src" / "__pycache__" / "token.cpython-312.pyc").write_bytes(b"\x00")
        commit(req.ctx.worktree, "aang-dev", "oops, bytecode")

    engine, rid = started(tmp_path, repo, FakeRunner({("toph", "write_tests"): leaves_junk,
                                                      ("aang", "implement"): commits_junk}))
    state = engine.run(rid, CARD)
    assert state["awaiting"]["what"] == "grouping"  # junk is not a scope violation
    wt = Path(state["worktree"])
    toph_files = sh(wt, "show", "--name-only", "--format=", ":/toph-sdet: commit pending").splitlines()
    assert toph_files == ["tests/test_token.py"]  # the orchestrator's auto-commit skips junk
    engine.approve(rid, CARD)
    state = engine.run(rid, CARD)
    official = sh(wt, "diff", "--name-only", f"main..{state['branches']['official']}").splitlines()
    assert sorted(official) == ["src/token.py", "tests/test_token.py"]


def test_concurrent_run_is_refused(tmp_path, repo):
    import os
    engine, rid = started(tmp_path, repo, FakeRunner())
    lock = tmp_path / "home" / "repos" / rid / "cards" / CARD / ".run.lock"
    lock.write_text(str(os.getpid()))  # a live process holds the card
    with pytest.raises(EngineError, match="already being run"):
        engine.run(rid, CARD)
    lock.write_text("999999999")  # stale lock from a dead process is taken over
    assert engine.run(rid, CARD)["awaiting"]["what"] == "grouping"
    assert not lock.exists()
