"""Reproducible install of agents-team on a new machine / KiroCrew account.

Everything machine-specific lives outside the repository; this module recreates it from the
templates and profiles under ``deploy/kiro/``:

- ``config.yml``             -> ``<state root>/config.yml`` (models per agent and phase)
- ``gitlab-mcp.env.example`` -> ``~/.config/kirocrew/gitlab-mcp.env`` (chmod 600, filled by a human)
- ``mcp/*.json``             -> MCP server entries in the KiroCrew agent config (``kirocrew.json``)
- ``profiles/<repo-id>/``    -> ``<state root>/repos/<repo-id>/discovery/`` so ``discover-apply`` works
  without paying for a new discovery run
- dashboard agents (``agents-team``, ``uncle-iroh``) via :mod:`kiro_install`

Rules: never overwrite an existing file or MCP entry unless ``force``; back up ``kirocrew.json``
before touching it; never read or print secret values (only whether a key is filled).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import discovery
from .adapters import kiro_install
from .schemas import errors, load
from .store import Layout

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy" / "kiro"
MIN_KIRO_CLI = (2, 19)
REQUIRED_ENV = ("GITLAB_PERSONAL_ACCESS_TOKEN", "GITLAB_API_URL")


@dataclass
class Report:
    ok: list[str] = field(default_factory=list)
    warn: list[str] = field(default_factory=list)
    fail: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return ([f"ok    {m}" for m in self.ok] + [f"warn  {m}" for m in self.warn]
                + [f"FAIL  {m}" for m in self.fail] + [f"todo  {m}" for m in self.todo])


@dataclass(frozen=True)
class Paths:
    """Every location the installer touches; all overridable for tests and other accounts."""

    layout: Layout
    agents_dir: Path = Path("~/.kiro/agents").expanduser()
    kirocrew_json: Path = Path("~/.kiro/agents/kirocrew.json").expanduser()
    env_file: Path = Path("~/.config/kirocrew/gitlab-mcp.env").expanduser()
    deploy_dir: Path = DEPLOY_DIR


# --- environment file ----------------------------------------------------------------------------

def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def ensure_env_file(paths: Paths, report: Report) -> None:
    target = paths.env_file
    if target.exists():
        report.ok.append(f"{target} exists (not changed)")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        shutil.copyfile(paths.deploy_dir / "gitlab-mcp.env.example", target)
        report.ok.append(f"created {target} from template")
    os.chmod(target, 0o600)
    values = read_env(target)
    missing = [k for k in REQUIRED_ENV if not values.get(k)]
    if missing:
        report.todo.append(f"fill {', '.join(missing)} in {target} (service account values)")


# --- KiroCrew MCP servers ------------------------------------------------------------------------

def mcp_templates(paths: Paths) -> dict[str, dict]:
    servers = {}
    for f in sorted((paths.deploy_dir / "mcp").glob("*.json")):
        servers[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    return servers


def _node_setup() -> str:
    """Shell prefix that puts npx on PATH: nvm's default alias when nvm is installed."""
    if (Path.home() / ".nvm" / "nvm.sh").exists():
        return 'export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" > /dev/null 2>&1 && '
    return ""


def render_mcp(template: dict) -> dict:
    rendered = json.loads(json.dumps(template))
    rendered["args"] = [a.replace("{node_setup}", _node_setup()) for a in rendered.get("args", [])]
    return rendered


def ensure_mcp(paths: Paths, report: Report, force: bool = False) -> None:
    cfg_path = paths.kirocrew_json
    if not cfg_path.is_file():
        report.warn.append(f"{cfg_path} not found: KiroCrew not set up yet; MCP servers not registered")
        return
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    servers = cfg.setdefault("mcpServers", {})
    changed = []
    for name, template in mcp_templates(paths).items():
        if name in servers and not force:
            report.ok.append(f"MCP server '{name}' already configured (not changed; --force-mcp to replace)")
            continue
        servers[name] = render_mcp(template)
        changed.append(name)
        if f"@{name}" not in cfg.setdefault("tools", []):
            cfg["tools"].append(f"@{name}")
    if changed:
        backup = paths.env_file.parent / f"kirocrew.json.bak-{datetime.now():%Y%m%d%H%M%S}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfg_path, backup)
        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        report.ok.append(f"registered MCP servers {changed} in {cfg_path} (backup {backup})")
        report.todo.append("restart KiroCrew so it loads the new MCP servers")


# --- team config and profiles --------------------------------------------------------------------

def ensure_team_config(paths: Paths, report: Report, force: bool = False) -> None:
    target = paths.layout.team_config()
    if target.exists() and not force:
        report.ok.append(f"{target} exists (not changed)")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(paths.deploy_dir / "config.yml", target)
        report.ok.append(f"wrote {target}")
    problems = errors("team-config", load(target))
    if problems:
        report.fail.append(f"{target} invalid: {'; '.join(problems)}")


def install_profiles(paths: Paths, report: Report, force: bool = False) -> None:
    root = paths.deploy_dir / "profiles"
    for profile in sorted(p for p in root.glob("*") if p.is_dir()):
        dest = paths.layout.repo_dir(profile.name) / "discovery"
        cfg = profile / "agents-team.yml"
        problems = errors("config", load(cfg))
        if problems:
            report.fail.append(f"profile {profile.name} invalid: {'; '.join(problems)}")
            continue
        if (dest / "agents-team.yml").exists() and not force:
            report.ok.append(f"profile {profile.name}: {dest} already has a proposal (not changed)")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("agents-team.yml", "conventions.md"):
            if (profile / name).exists():
                shutil.copyfile(profile / name, dest / name)
        meta = load(profile / "profile.yml") if (profile / "profile.yml").exists() else {}
        hint = meta.get("clone", "git clone <remote>")
        report.ok.append(f"profile {profile.name} staged in {dest}")
        report.todo.append(f"{profile.name}: `{hint}`, then `agents-team discover-apply --repo <clone path>`")


def export_profile(repo: Path, layout: Layout, out_root: Path, clone_hint: str = "") -> Path:
    """Copy a repository's applied config and conventions into ``deploy/kiro/profiles/<repo-id>``."""
    repo = repo.resolve()
    cfg = repo / ".agents-team.yml"
    if not cfg.exists():
        raise FileNotFoundError(f"{cfg} not found: run discover / discover-apply first")
    problems = errors("config", load(cfg))
    if problems:
        raise ValueError(f"{cfg} invalid: {'; '.join(problems)}")
    remote = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                            capture_output=True, text=True).stdout.strip()
    rid = discovery.repo_id(remote, repo.name)
    dest = out_root / rid
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cfg, dest / "agents-team.yml")
    conventions = layout.memory_dir(rid) / "conventions.md"
    if conventions.exists():
        shutil.copyfile(conventions, dest / "conventions.md")
    clean_remote = re.sub(r"//[^/@]+@", "//", remote)  # never export credentials embedded in a URL
    (dest / "profile.yml").write_text(
        f"repo_id: {rid}\nremote: {clean_remote}\nclone: {clone_hint or clean_remote}\n", encoding="utf-8")
    return dest


# --- prerequisites -------------------------------------------------------------------------------

def _version(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check_prereqs(paths: Paths, report: Report, network: bool = True) -> None:
    for exe in ("git", "uv", "kiro-cli"):
        if shutil.which(exe) is None:
            report.fail.append(f"{exe} not on PATH")
    if shutil.which("kiro-cli"):
        out = _version(["kiro-cli", "--version"])
        match = re.search(r"(\d+)\.(\d+)", out)
        if match and tuple(map(int, match.groups())) < MIN_KIRO_CLI:
            report.fail.append(f"kiro-cli {out} is older than {'.'.join(map(str, MIN_KIRO_CLI))} "
                               "(permission behavior verified on 2.19)")
        else:
            report.ok.append(f"kiro-cli {out or '?'}")
    if shutil.which("kirocrew") is None:
        report.warn.append("kirocrew not on PATH: dashboard agents need KiroCrew; the CLI works without it")
    if shutil.which("agents-team") is None:
        report.todo.append(f"uv tool install -e {REPO_ROOT}   # puts agents-team on PATH")
    url = read_env(paths.env_file).get("GITLAB_API_URL")
    if network and url:
        report_gitlab(url, report)


def report_gitlab(url: str, report: Report, timeout: float = 8.0) -> None:
    """Reachability only (no token sent). GAS GitLab answers only inside the VPN."""
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=timeout,
                               context=ssl.create_default_context())
        report.ok.append(f"GitLab reachable at {url}")
    except urllib.error.HTTPError as exc:  # any HTTP answer means the network path works
        report.ok.append(f"GitLab reachable at {url} (HTTP {exc.code})")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        report.fail.append(f"GitLab not reachable at {url} ({exc}); is the VPN up?")


# --- entry point ---------------------------------------------------------------------------------

def install(paths: Paths, *, force: bool = False, force_mcp: bool = False, register: bool = True,
            network: bool = True) -> Report:
    report = Report()
    check_prereqs(paths, report, network=False)
    ensure_team_config(paths, report, force=force)
    ensure_env_file(paths, report)
    ensure_mcp(paths, report, force=force_mcp)
    for line in kiro_install.install(paths.agents_dir, paths.layout, paths.kirocrew_json, register):
        report.ok.append(line)
    install_profiles(paths, report, force=force)
    url = read_env(paths.env_file).get("GITLAB_API_URL")
    if network and url:
        report_gitlab(url, report)
    return report


def doctor(paths: Paths, network: bool = True) -> Report:
    report = Report()
    check_prereqs(paths, report, network=network)
    team = paths.layout.team_config()
    if not team.exists():
        report.fail.append(f"{team} missing: run agents-team install")
    elif problems := errors("team-config", load(team)):
        report.fail.append(f"{team} invalid: {'; '.join(problems)}")
    else:
        report.ok.append(f"{team} valid")
    values = read_env(paths.env_file)
    missing = [k for k in REQUIRED_ENV if not values.get(k)]
    if not paths.env_file.exists():
        report.fail.append(f"{paths.env_file} missing")
    elif missing:
        report.fail.append(f"{paths.env_file}: {', '.join(missing)} empty")
    elif paths.env_file.stat().st_mode & 0o077:
        report.fail.append(f"{paths.env_file} is readable by others: chmod 600 it")
    else:
        report.ok.append(f"{paths.env_file} filled (values not shown)")
    for name in ("agents-team.json", "uncle-iroh.json"):
        p = paths.agents_dir / name
        (report.ok if p.exists() else report.fail).append(f"{p} {'present' if p.exists() else 'missing'}")
    if paths.kirocrew_json.is_file():
        servers = json.loads(paths.kirocrew_json.read_text(encoding="utf-8")).get("mcpServers", {})
        for name in mcp_templates(paths):
            (report.ok if name in servers else report.fail).append(
                f"MCP server '{name}' {'registered' if name in servers else 'not registered'}")
    return report
