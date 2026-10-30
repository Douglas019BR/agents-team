import subprocess
from pathlib import Path

import pytest
import yaml

from agents_team import discovery


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    for f in ["src/app/token.py", "tests/test_token.py", "features/login.feature", "features/steps/login.py", ".env"]:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text("x\n")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    _git(root.parent, "init", "-q", str(root))
    _git(root, "remote", "add", "origin", "git@gitlab.example.com:grp/sub/proj.git")
    return root


def _proposal(tmp_path, **overrides):
    cfg = {
        "version": 1,
        "stack": {"language": "python", "bdd": "behave"},
        "commands": {"test": "git status", "test_single": "git log {target}"},
        "paths": {
            "source": ["src/**"],
            "tests": ["tests/**"],
            "features": ["features/**/*.feature"],
            "steps": ["features/steps/**"],
            "protected": [".env*"],
        },
    }
    for key, value in overrides.items():
        cfg[key] = value
    out = tmp_path / "proposal"
    out.mkdir(exist_ok=True)
    (out / "agents-team.yml").write_text(yaml.safe_dump(cfg))
    (out / "conventions.md").write_text("# Conventions\n")
    return out


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@gitlab.example.com:grp/sub/proj.git", "grp-sub-proj"),
        ("https://gitlab.example.com/grp/proj.git", "grp-proj"),
        ("https://user:tok@gitlab.example.com/Grp/Proj", "grp-proj"),
        ("", "my-repo"),
    ],
)
def test_repo_id(url, expected):
    assert discovery.repo_id(url, "My Repo") == expected


def test_facts(repo):
    f = discovery.facts(repo)
    assert f["repo_id"] == "grp-sub-proj"
    assert "pyproject.toml" in f["root_files"]
    assert f["has_features_dir"] is True
    assert set(f["scanners"]) == set(discovery.SCANNERS)


def test_facts_redacts_credentials_in_remote(repo):
    _git(repo, "remote", "set-url", "origin", "https://user:secret@gitlab.example.com/g/p.git")
    assert "secret" not in discovery.facts(repo)["remote"]


def test_check_accepts_valid_proposal(repo, tmp_path):
    problems, _ = discovery.check(repo, _proposal(tmp_path) / "agents-team.yml")
    assert problems == []


def test_check_rejects_control_chars(repo, tmp_path):
    proposal = _proposal(tmp_path, commands={"test": "git status && git log"})
    problems, _ = discovery.check(repo, proposal / "agents-team.yml")
    assert any("control characters" in p for p in problems)


def test_check_rejects_unknown_executable(repo, tmp_path):
    proposal = _proposal(tmp_path, commands={"test": "definitely-not-a-binary run"})
    problems, _ = discovery.check(repo, proposal / "agents-team.yml")
    assert any("not found" in p for p in problems)


def test_check_rejects_overlapping_source_and_tests(repo, tmp_path):
    paths = {"source": ["**/*.py"], "tests": ["tests/**"]}
    problems, _ = discovery.check(repo, _proposal(tmp_path, paths=paths) / "agents-team.yml")
    assert any("overlap" in p for p in problems)


def test_check_rejects_features_matching_steps(repo, tmp_path):
    paths = {"source": ["src/**"], "tests": ["tests/**"], "features": ["features/**"], "steps": ["features/steps/**"]}
    problems, _ = discovery.check(repo, _proposal(tmp_path, paths=paths) / "agents-team.yml")
    assert any("features and paths.steps overlap" in p for p in problems)


def test_check_rejects_missing_scanner(repo, tmp_path):
    proposal = _proposal(tmp_path, security={"tools": ["trivy"]})
    problems, _ = discovery.check(repo, proposal / "agents-team.yml")
    if discovery.shutil.which("trivy") is None:
        assert any("trivy" in p for p in problems)


def test_check_warns_on_empty_glob(repo, tmp_path):
    paths = {"source": ["lib/**"], "tests": ["tests/**"]}
    _, warnings = discovery.check(repo, _proposal(tmp_path, paths=paths) / "agents-team.yml")
    assert any("paths.source" in w for w in warnings)


def test_apply_writes_config_excludes_it_and_is_safe(repo, tmp_path):
    memory = tmp_path / "state" / "memory"
    done = discovery.apply(repo, _proposal(tmp_path), memory)
    assert (repo / ".agents-team.yml").exists()
    assert (memory / "conventions.md").exists()
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert "/.agents-team.yml" in exclude and "/.kiro/" in exclude
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", ".agents-team.yml"], capture_output=True, text=True).stdout
    assert status == ""  # ignored, never shows up for commit
    assert any("excluded" in line for line in done)
    with pytest.raises(FileExistsError):
        discovery.apply(repo, _proposal(tmp_path), memory)
    assert discovery.ensure_excluded(repo) == []  # idempotent


def test_apply_refuses_invalid_proposal(repo, tmp_path):
    proposal = _proposal(tmp_path, commands={"test": "git status; id"})
    with pytest.raises(ValueError):
        discovery.apply(repo, proposal, tmp_path / "m")
    assert not (repo / ".agents-team.yml").exists()


def test_exclude_file_shared_by_worktrees(repo, tmp_path):
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt))
    assert discovery.exclude_file(wt) == discovery.exclude_file(repo)


def test_facts_report_toolchain(repo):
    assert set(discovery.facts(repo)["executables"]) == set(discovery.TOOLCHAIN)
