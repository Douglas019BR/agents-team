import json
import stat
import subprocess
from pathlib import Path

import pytest

from agents_team import deploy
from agents_team.schemas import errors, load
from agents_team.store import Layout

DEPLOY = deploy.DEPLOY_DIR


@pytest.fixture
def paths(tmp_path):
    kirocrew = tmp_path / "agents" / "kirocrew.json"
    kirocrew.parent.mkdir(parents=True)
    kirocrew.write_text(json.dumps({"name": "kirocrew", "tools": ["read"],
                                    "mcpServers": {"other": {"command": "x"}}}))
    return deploy.Paths(layout=Layout(tmp_path / "home"), agents_dir=tmp_path / "agents",
                        kirocrew_json=kirocrew, env_file=tmp_path / "config" / "kirocrew" / "gitlab-mcp.env")


# --- what ships in the repository ---------------------------------------------------------------

def test_shipped_templates_are_valid_and_secret_free():
    assert errors("team-config", load(DEPLOY / "config.yml")) == []
    env = deploy.read_env(DEPLOY / "gitlab-mcp.env.example")
    assert env["GITLAB_PERSONAL_ACCESS_TOKEN"] == ""  # never ship a token
    for f in (DEPLOY / "mcp").glob("*.json"):
        server = json.loads(f.read_text())
        assert "command" in server and "args" in server
        assert not any(k.endswith(("TOKEN", "SECRET", "PASSWORD")) and v for k, v in server.get("env", {}).items())


def test_gitlab_mcp_template_is_read_only_and_loads_env_file():
    server = json.loads((DEPLOY / "mcp" / "gitlab-gastecnologia.json").read_text())
    assert server["env"]["GITLAB_READ_ONLY_MODE"] == "true"
    assert "gitlab-mcp.env" in server["args"][1]
    assert "GITLAB_PERSONAL_ACCESS_TOKEN" not in json.dumps(server)  # comes from the env file


@pytest.mark.parametrize("profile", sorted(p.name for p in (DEPLOY / "profiles").glob("*") if p.is_dir()))
def test_shipped_profiles_are_valid_and_generic(profile):
    cfg = load(DEPLOY / "profiles" / profile / "agents-team.yml")
    assert errors("config", cfg) == []
    assert not cfg.get("workflow", {}).get("sdet") == "skip"  # review-fix mode is per card, not per repo
    assert "{card}" in cfg["branches"]["official"]
    text = (DEPLOY / "profiles" / profile / "conventions.md").read_text()
    assert "this card" not in text and "this run" not in text


# --- install ---------------------------------------------------------------------------------------

def test_install_sets_up_everything(paths):
    report = deploy.install(paths, register=False, network=False)
    assert not report.fail, report.lines()
    assert paths.layout.team_config().exists()
    assert stat.S_IMODE(paths.env_file.stat().st_mode) == 0o600
    assert any("fill GITLAB_PERSONAL_ACCESS_TOKEN" in t for t in report.todo)
    cfg = json.loads(paths.kirocrew_json.read_text())
    assert {"gitlab-gastecnologia", "atlassian-rovo-mcp", "other"} <= set(cfg["mcpServers"])
    assert "@gitlab-gastecnologia" in cfg["tools"]
    assert "{node_setup}" not in json.dumps(cfg)
    assert list(paths.env_file.parent.glob("kirocrew.json.bak-*"))  # backup before editing
    assert (paths.agents_dir / "agents-team.json").exists() and (paths.agents_dir / "uncle-iroh.json").exists()
    for profile in (DEPLOY / "profiles").glob("*"):
        assert (paths.layout.repo_dir(profile.name) / "discovery" / "agents-team.yml").exists()


def test_install_is_idempotent_and_never_clobbers(paths):
    deploy.install(paths, register=False, network=False)
    paths.env_file.write_text("GITLAB_PERSONAL_ACCESS_TOKEN=secret-value\nGITLAB_API_URL=https://x\n")
    custom = paths.layout.team_config().read_text().replace("glm-5", "my-model")
    paths.layout.team_config().write_text(custom)
    cfg = json.loads(paths.kirocrew_json.read_text())
    cfg["mcpServers"]["gitlab-gastecnologia"]["env"]["GITLAB_READ_ONLY_MODE"] = "false"  # user's own choice
    paths.kirocrew_json.write_text(json.dumps(cfg))

    report = deploy.install(paths, register=False, network=False)
    assert "secret-value" in paths.env_file.read_text()
    assert "my-model" in paths.layout.team_config().read_text()
    after = json.loads(paths.kirocrew_json.read_text())
    assert after["mcpServers"]["gitlab-gastecnologia"]["env"]["GITLAB_READ_ONLY_MODE"] == "false"
    assert not any("fill" in t for t in report.todo)
    assert "secret-value" not in "\n".join(report.lines())  # values are never echoed


def test_force_mcp_replaces_entry(paths):
    deploy.install(paths, register=False, network=False)
    cfg = json.loads(paths.kirocrew_json.read_text())
    cfg["mcpServers"]["gitlab-gastecnologia"] = {"command": "old"}
    paths.kirocrew_json.write_text(json.dumps(cfg))
    deploy.install(paths, register=False, network=False, force_mcp=True)
    assert json.loads(paths.kirocrew_json.read_text())["mcpServers"]["gitlab-gastecnologia"]["command"] == "/bin/bash"


def test_install_without_kirocrew_warns_but_works(paths):
    paths.kirocrew_json.unlink()
    report = deploy.install(paths, register=False, network=False)
    assert not report.fail
    assert any("KiroCrew not set up" in w for w in report.warn)


# --- doctor -----------------------------------------------------------------------------------------

def test_doctor_reports_missing_and_ready(paths):
    report = deploy.doctor(paths, network=False)
    assert any("config.yml missing" in f for f in report.fail)
    deploy.install(paths, register=False, network=False)
    assert any("empty" in f for f in deploy.doctor(paths, network=False).fail)
    paths.env_file.write_text("GITLAB_PERSONAL_ACCESS_TOKEN=x\nGITLAB_API_URL=https://x\n")
    paths.env_file.chmod(0o644)
    assert any("readable by others" in f for f in deploy.doctor(paths, network=False).fail)
    paths.env_file.chmod(0o600)
    ready = deploy.doctor(paths, network=False)
    assert not [f for f in ready.fail if "PATH" not in f and "kiro-cli" not in f], ready.lines()


def test_report_gitlab_unreachable_mentions_vpn():
    report = deploy.Report()
    deploy.report_gitlab("https://127.0.0.1:9", report, timeout=1)
    assert report.fail and "VPN" in report.fail[0]


# --- export ------------------------------------------------------------------------------------------

def test_export_profile_strips_credentials_from_remote(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://bot:tok123@gitlab.example.com/g/proj.git"],
                   check=True)
    (repo / ".agents-team.yml").write_text(
        "version: 1\nstack: {language: python}\ncommands: {test: 'git status'}\n"
        "paths: {source: ['src/**'], tests: ['tests/**']}\n")
    layout = Layout(tmp_path / "home")
    layout.memory_dir("g-proj").mkdir(parents=True)
    (layout.memory_dir("g-proj") / "conventions.md").write_text("# c\n")
    dest = deploy.export_profile(repo, layout, tmp_path / "out")
    assert dest.name == "g-proj"
    assert (dest / "conventions.md").exists()
    assert "tok123" not in (dest / "profile.yml").read_text()


def test_export_profile_requires_valid_config(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(FileNotFoundError):
        deploy.export_profile(repo, Layout(tmp_path / "h"), tmp_path / "out")
    (repo / ".agents-team.yml").write_text("commands: {}\n")
    with pytest.raises(ValueError):
        deploy.export_profile(repo, Layout(tmp_path / "h"), tmp_path / "out")
