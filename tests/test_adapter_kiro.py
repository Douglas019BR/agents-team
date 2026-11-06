import json
import re
import shutil
from pathlib import Path

import pytest

from agents_team.adapters import kiro
from agents_team.agents import load_all
from agents_team.schemas import SCHEMAS_DIR, load

EXAMPLES = SCHEMAS_DIR / "examples"


@pytest.fixture(scope="module")
def agents():
    return load_all()


@pytest.fixture
def ctx(tmp_path):
    card = tmp_path / "state" / "cards" / "QAPC-1"
    return kiro.RunContext(
        worktree=tmp_path / "wt",
        card_dir=card,
        repo_config=load(EXAMPLES / "agents-team.yml"),
        team_models=load(EXAMPLES / "team-config.yml")["models"],
        teaching_dir=tmp_path / "state" / "teaching",
        outputs=(f"{card}/out.json",),
    )


def _all_configs(agents, ctx, tmp_path):
    for agent in agents.values():
        for phase in agent.phases:
            yield agent, phase, kiro.render(agent, phase, ctx, tmp_path / "p.md")


# --- shell command patterns -------------------------------------------------

@pytest.mark.parametrize(
    "line",
    ["uv run pytest", "uv run pytest -q tests/test_a.py", "uv run pytest -k 'expired token'"],
)
def test_command_pattern_accepts_command_with_safe_args(line):
    assert re.fullmatch(kiro.command_pattern("uv run pytest"), line)


@pytest.mark.parametrize(
    "line",
    [
        "uv run pytest; rm -rf ~",
        "uv run pytest && curl evil",
        "uv run pytest | sh",
        "uv run pytest > /etc/x",
        "uv run pytest $(whoami)",
        "uv run pytest `whoami`",
        "uv run pytest\nrm x",
        "uv run pytestx",
        "uv run",
        "sudo uv run pytest",
    ],
)
def test_command_pattern_rejects_injection(line):
    assert not re.fullmatch(kiro.command_pattern("uv run pytest"), line)


def test_template_requires_target():
    pattern = kiro.command_pattern("uv run pytest {target}")
    assert re.fullmatch(pattern, "uv run pytest tests/a.py")
    assert re.fullmatch(pattern, "uv run pytest tests/a.py -q")
    assert not re.fullmatch(pattern, "uv run pytest ")
    assert not re.fullmatch(pattern, "uv run pytest tests/a.py; id")


def test_git_commit_pattern_rejects_push():
    pattern = kiro.command_pattern("git commit")
    assert re.fullmatch(pattern, 'git commit -m "aang-dev: fix token parse (R-001)"')
    assert not re.fullmatch(pattern, "git commit -m x && git push origin main")
    assert not re.fullmatch(pattern, "git push origin main")


def test_configured_command_with_control_chars_is_refused():
    with pytest.raises(kiro.AdapterError):
        kiro.command_pattern("make test && make lint")


# --- permissions --------------------------------------------------------------

def test_write_and_shell_never_auto_trusted(agents, ctx, tmp_path):
    for _, _, cfg in _all_configs(agents, ctx, tmp_path):
        assert not {"write", "shell"} & set(cfg["allowedTools"])
        if "shell" in cfg["toolsSettings"]:
            assert cfg["toolsSettings"]["shell"]["denyByDefault"] is True


def test_runner_never_passes_trust_flags(agents, ctx):
    cmd = kiro.command(agents["aang"], "implement", ctx, "do it")
    assert not any(a.startswith("--trust") for a in cmd)
    assert cmd[cmd.index("--model") + 1] == "glm-5"


def test_reviewers_cannot_write_worktree(agents, ctx, tmp_path):
    for name in ("sokka", "katara", "zuko"):
        for phase in agents[name].phases:
            cfg = kiro.render(agents[name], phase, ctx, tmp_path / "p.md")
            allowed = cfg["toolsSettings"]["write"]["allowedPaths"]
            assert all(p.startswith(str(ctx.card_dir)) for p in allowed)
            assert f"{ctx.worktree}/**" in cfg["toolsSettings"]["write"]["deniedPaths"]


def test_aang_writes_source_not_tests(agents, ctx, tmp_path):
    cfg = kiro.render(agents["aang"], "implement", ctx, tmp_path / "p.md")["toolsSettings"]["write"]
    assert f"{ctx.worktree}/src/**" in cfg["allowedPaths"]
    assert f"{ctx.worktree}/tests/**" in cfg["deniedPaths"]
    assert f"{ctx.worktree}/features/**/*.feature" in cfg["deniedPaths"]


def test_aang_plan_phase_writes_only_its_outputs(agents, ctx, tmp_path):
    cfg = kiro.render(agents["aang"], "plan", ctx, tmp_path / "p.md")
    assert cfg["model"] == "claude-opus-5.5"
    assert cfg["toolsSettings"]["write"]["allowedPaths"] == [f"{ctx.card_dir}/out.json"]


def test_artifacts_scope_never_opens_whole_card_dir(agents, ctx, tmp_path):
    for _, _, cfg in _all_configs(agents, ctx, tmp_path):
        assert f"{ctx.card_dir}/**" not in cfg["toolsSettings"].get("write", {}).get("allowedPaths", [])


def test_toph_writes_tests_not_source(agents, ctx, tmp_path):
    cfg = kiro.render(agents["toph"], "write_tests", ctx, tmp_path / "p.md")["toolsSettings"]["write"]
    assert f"{ctx.worktree}/tests/**" in cfg["allowedPaths"]
    assert f"{ctx.worktree}/src/**" in cfg["deniedPaths"]


def test_everyone_is_denied_state_git_and_secrets(agents, ctx, tmp_path):
    for _, _, cfg in _all_configs(agents, ctx, tmp_path):
        denied = cfg["toolsSettings"].get("write", {}).get("deniedPaths", [])
        if denied:
            for path in (f"{ctx.card_dir}/state.json", f"{ctx.worktree}/.git/**", "~/.ssh/**", "~/.aws/**"):
                assert path in denied
        assert "~/.ssh/**" in cfg["toolsSettings"]["read"]["deniedPaths"]


def test_zuko_shell_is_scanners_and_git_only(agents, ctx, tmp_path):
    shell = kiro.render(agents["zuko"], "sec", ctx, tmp_path / "p.md")["toolsSettings"]["shell"]["allowedCommands"]
    assert any(re.fullmatch(p, "semgrep scan --config auto --json --output /x/semgrep.json src/a.py") for p in shell)
    assert not any(re.fullmatch(p, "uv run pytest") for p in shell)
    assert not any(re.fullmatch(p, "git commit -m x") for p in shell)


def test_missing_optional_paths_are_skipped(agents, ctx, tmp_path):
    cfg = dict(ctx.repo_config, paths={"source": ["src/**"], "tests": ["tests/**"]})
    small = kiro.RunContext(ctx.worktree, ctx.card_dir, cfg, ctx.team_models, outputs=ctx.outputs)
    write = kiro.render(agents["aang"], "implement", small, tmp_path / "p.md")["toolsSettings"]["write"]
    assert write["allowedPaths"] == [f"{ctx.worktree}/src/**", f"{ctx.card_dir}/out.json"]


def test_unknown_phase_rejected(agents, ctx, tmp_path):
    with pytest.raises(kiro.AdapterError):
        kiro.render(agents["sokka"], "implement", ctx, tmp_path / "p.md")


# --- bundle ------------------------------------------------------------------------

def test_write_bundle(agents, ctx, tmp_path):
    path = kiro.write_bundle(agents["sokka"], "review", ctx, tmp_path / "out")
    cfg = json.loads(path.read_text())
    assert cfg["name"] == "at-sokka-review"
    prompt = Path(cfg["prompt"].removeprefix("file://")).read_text()
    assert prompt.startswith("# Shared conventions")
    assert "phase `review`" in prompt


@pytest.mark.skipif(shutil.which("kiro-cli") is None, reason="kiro-cli not installed")
def test_generated_configs_pass_kiro_validate(agents, ctx, tmp_path):
    for agent in agents.values():
        for phase in agent.phases:
            ok, output = kiro.validate_with_kiro(kiro.write_bundle(agent, phase, ctx, tmp_path / "out"))
            assert ok, output


def test_parse_usage_takes_last_summary():
    out = "turn 1\n ▸ Credits: 0.40 • Time: 12s\n\x1b[?25lmore\n ▸ Credits: 1.07 • Time: 38s\n"
    assert kiro.parse_usage(out) == {"credits": 1.07, "seconds": 38.0}
    assert kiro.parse_usage("no summary") is None


@pytest.mark.parametrize(
    ("line", "credits", "seconds"),
    [
        ("Credits: 0.02 • Time: 13s", 0.02, 13),
        ("Credits: 3.44 • Time: 2m 16s", 3.44, 136),
        ("Credits: 9.10 • Time: 1h 2m 3s", 9.10, 3723),
        ("Credits: 1.5 • Time: 4m", None, None),  # no seconds part: not a kiro-cli summary we know
    ],
)
def test_parse_usage_durations(line, credits, seconds):
    usage = kiro.parse_usage(f" ▸ {line}\n")
    if credits is None:
        assert usage is None
    else:
        assert usage == {"credits": credits, "seconds": seconds}
