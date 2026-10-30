from pathlib import Path

import pytest

from agents_team.agents import AgentDefinitionError, load_all, parse
from agents_team.schemas import SCHEMAS_DIR, load

EXPECTED = {"aang", "toph", "sokka", "katara", "zuko", "uncle-iroh"}
REVIEWERS = {"sokka", "katara", "zuko", "uncle-iroh"}


@pytest.fixture(scope="module")
def agents():
    return load_all()


def test_all_agents_load(agents):
    assert set(agents) == EXPECTED


def test_only_dev_and_sdet_commit(agents):
    assert {n for n, a in agents.items() if a.meta["permissions"]["git_commit"]} == {"aang", "toph"}


@pytest.mark.parametrize("name", sorted(REVIEWERS))
def test_read_only_agents_cannot_write_repo(agents, name):
    perms = agents[name].meta["permissions"]
    assert "repo" in perms["deny_write"]
    for scopes in perms["write"].values():
        assert not any(s == "repo" or s.startswith("paths.") for s in scopes)


def test_aang_cannot_write_tests_or_features(agents):
    deny = agents["aang"].meta["permissions"]["deny_write"]
    assert {"paths.tests", "paths.features", "paths.protected"} <= set(deny)


def test_toph_cannot_write_source_or_steps(agents):
    deny = agents["toph"].meta["permissions"]["deny_write"]
    assert {"paths.source", "paths.steps", "paths.protected"} <= set(deny)


def test_no_agent_has_mcp_write(agents):
    for agent in agents.values():
        assert set(agent.meta["permissions"]["mcp"].values()) <= {"read"}


def test_teacher_is_human_only(agents):
    assert agents["uncle-iroh"].meta["invocation"] == "human"
    assert all(a.meta["invocation"] == "orchestrator" for n, a in agents.items() if n != "uncle-iroh")


def test_team_config_covers_every_agent_phase(agents):
    team = load(SCHEMAS_DIR / "examples" / "team-config.yml")
    for agent in agents.values():
        for phase in agent.phases:
            assert agent.model_for(phase, team["models"])


def test_dev_and_reviewer_use_different_models(agents):
    team = load(SCHEMAS_DIR / "examples" / "team-config.yml")["models"]
    reviewer = agents["sokka"].model_for("review", team)
    for phase in agents["aang"].phases:
        assert agents["aang"].model_for(phase, team) != reviewer


def test_model_resolution_falls_back_to_default(agents):
    assert agents["aang"].model_for("plan", {}) == "claude-opus-5.5"
    assert agents["aang"].model_for("implement", {"aang": "x"}) == "x"
    with pytest.raises(AgentDefinitionError):
        agents["aang"].model_for("teach", {})


def test_prompt_includes_shared_conventions(agents):
    prompt = agents["sokka"].prompt()
    assert prompt.startswith("# Shared conventions")
    assert "# Sokka (reviewer)" in prompt


def _write(tmp_path: Path, name: str, front: str) -> Path:
    path = tmp_path / f"{name}.md"
    path.write_text(f"---\n{front}---\n# body\n", encoding="utf-8")
    return path


BASE = """name: {name}
role: qa
version: 0.1.0
description: test
invocation: orchestrator
phases: [qa]
default_model: m
permissions:
  read: [repo]
  write: {write}
  deny_write: [repo]
  shell: [{shell}]
  git_commit: {commit}
  mcp: {{}}
inputs: []
outputs: []
"""


def test_rejects_file_name_mismatch(tmp_path):
    path = _write(tmp_path, "other", BASE.format(name="x", write="{}", shell="git:diff", commit="false"))
    with pytest.raises(AgentDefinitionError, match="file name"):
        parse(path)


def test_rejects_write_scope_for_unknown_phase(tmp_path):
    path = _write(tmp_path, "x", BASE.format(name="x", write="{fly: [artifacts]}", shell="git:diff", commit="false"))
    with pytest.raises(AgentDefinitionError, match="unknown phases"):
        parse(path)


def test_rejects_commit_flag_mismatch(tmp_path):
    path = _write(tmp_path, "x", BASE.format(name="x", write="{}", shell="git:commit", commit="false"))
    with pytest.raises(AgentDefinitionError, match="git_commit"):
        parse(path)


def test_rejects_mcp_write(tmp_path):
    front = BASE.format(name="x", write="{}", shell="git:diff", commit="false").replace("mcp: {}", "mcp: {jira: write}")
    with pytest.raises(AgentDefinitionError):
        parse(_write(tmp_path, "x", front))
