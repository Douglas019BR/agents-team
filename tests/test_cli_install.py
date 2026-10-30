import json
import re
import subprocess

import pytest

from agents_team import cli
from agents_team.adapters import kiro_install
from agents_team.store import Layout


@pytest.fixture
def installed(tmp_path):
    src = tmp_path / "kirocrew.json"
    src.write_text(json.dumps({"mcpServers": {"atlassian-rovo-mcp": {"command": "zsh", "args": ["-c", "x"]},
                                              "other": {"command": "y"}}}))
    agents_dir = tmp_path / "agents"
    lines = kiro_install.install(agents_dir, Layout(tmp_path / "home"), src, register=False)
    return agents_dir, lines, tmp_path / "home"


def test_install_writes_entry_and_iroh(installed):
    agents_dir, lines, home = installed
    entry = json.loads((agents_dir / "agents-team.json").read_text())
    iroh = json.loads((agents_dir / "at-uncle-iroh-teach.json").read_text())
    assert (home / "inbox").is_dir() and (home / "teaching").is_dir()
    assert list(entry["mcpServers"]) == ["atlassian-rovo-mcp"]  # only the Jira server is copied
    assert iroh["toolsSettings"]["write"]["allowedPaths"] == [f"{home / 'teaching'}/**"]
    assert "{" not in (agents_dir / "agents-team.prompt.md").read_text().split("## Locations")[1]


def test_entry_never_auto_trusts_writes(installed):
    entry = json.loads((installed[0] / "agents-team.json").read_text())
    allowed = set(entry["allowedTools"])
    assert not {"write", "shell"} & allowed
    for tool in kiro_install.JIRA_WRITE:
        assert f"@atlassian-rovo-mcp/{tool}" in entry["tools"]
        assert f"@atlassian-rovo-mcp/{tool}" not in allowed
    assert f"{installed[2]}/repos/**" in entry["toolsSettings"]["write"]["deniedPaths"]


@pytest.mark.parametrize(
    ("line", "ok"),
    [
        ("agents-team status --repo /x --card QAPC-1", True),
        ("agents-team run --repo /x --card QAPC-1 --background", True),
        ("agents-team discover --repo /x", True),
        ("agents-team status --repo /x --card Q; rm -rf ~", False),
        ("agents-team status --repo /x && git push origin main", False),
        ("git push origin main", False),
        ("agents-teamx status", False),
    ],
)
def test_entry_shell_allowlist(installed, line, ok):
    entry = json.loads((installed[0] / "agents-team.json").read_text())
    patterns = entry["toolsSettings"]["shell"]["allowedCommands"]
    assert any(re.fullmatch(p, line) for p in patterns) is ok


def test_install_without_atlassian_disables_jira(tmp_path):
    lines = kiro_install.install(tmp_path / "agents", Layout(tmp_path / "home"), tmp_path / "missing.json", False)
    entry = json.loads((tmp_path / "agents" / "agents-team.json").read_text())
    assert "mcpServers" not in entry and "Jira tools disabled" in lines[0]


def test_cli_start_and_status(tmp_path, capsys):
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    (repo / ".agents-team.yml").write_text(
        "version: 1\nstack: {language: python}\ncommands: {test: 'git status'}\n"
        "paths: {source: ['src/**'], tests: ['tests/**']}\n")
    card = tmp_path / "c.md"
    card.write_text("# Título\n")
    home = str(tmp_path / "home")
    assert cli.main(["--home", home, "start", "--repo", str(repo), "--card", "QAPC-9", "--card-file", str(card)]) == 0
    assert cli.main(["--home", home, "status", "--repo", str(repo), "--card", "QAPC-9", "--json"]) == 0
    assert '"phase": "created"' in capsys.readouterr().out
    assert cli.main(["--home", home, "approve", "--repo", str(repo), "--card", "QAPC-9"]) == 2
    assert cli.main(["--home", home, "discover-apply", "--repo", str(repo)]) == 2  # exists, no --force
