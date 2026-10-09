"""End-to-end tests on real temp directories (no mocks).

Each test builds a fake HOME with real files in Claude Code's layout, runs the
real scripts, and inspects what landed on disk.
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "migrate-from-claude" / "scripts"


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run(*args, env_home=None):
    res = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "HOME": str(env_home or "/nonexistent")})
    assert res.returncode == 0, res.stderr
    return res.stdout


def build_home(tmp: Path) -> tuple[Path, Path]:
    home, proj = tmp / "home", tmp / "work" / "svc"
    c = home / ".claude"
    write(c / "CLAUDE.md", "Always write tests first.\n")
    write(c / "skills" / "deploy" / "SKILL.md", "---\nname: deploy\ndescription: Deploy the app.\n---\nRun make deploy.\n")
    write(c / "skills" / "deploy" / "scripts" / "go.sh", "echo deploy\n")
    write(c / "commands" / "fixit.md", "---\ndescription: Fix the issue\n---\nFix $ARGUMENTS now.\n")
    write(c / "agents" / "reviewer.md",
          "---\nname: reviewer\ndescription: Reviews diffs\ntools: Read, Grep, Bash, NotebookEdit\nmodel: opus\n---\nReview carefully.\n")
    write(c / "settings.json", json.dumps({
        "model": "opus",
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "~/h/check.sh"}]}],
                  "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        "permissions": {"allow": ["Bash(npm test)", "WebFetch(domain:example.com)"]}}))
    write(c / ".credentials.json", '{"token": "do-not-copy"}')
    write(c / "projects" / "-x" / "abc.jsonl", '{"type":"user"}\n')
    # Claude Code 2.x plugin cache layout, two versions
    for ver in ("1.0.0", "1.2.0"):
        root = c / "plugins" / "cache" / "mp" / "tools" / ver
        write(root / ".claude-plugin" / "plugin.json", json.dumps({"name": "tools", "version": ver}))
        write(root / "skills" / "lint" / "SKILL.md", f"---\ndescription: Lint v{ver}\n---\nlint\n")
        write(root / ".mcp.json", json.dumps({"mcpServers": {"docs": {"type": "http", "url": "https://example.com/mcp"}}}))
        write(root / "hooks" / "hooks.json", json.dumps({"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "fmt.sh"}]}]}}))
    write(home / ".claude.json", json.dumps({
        "mcpServers": {"local": {"command": "npx", "args": ["-y", "srv"], "env": {"K": "v"}}},
        "projects": {str(proj): {"mcpServers": {"projsrv": {"command": "uvx", "args": ["p"]}}}}}))
    write(proj / "CLAUDE.md", "Use Go 1.x idioms.\n")
    write(proj / ".mcp.json", json.dumps({"mcpServers": {"db": {"command": "db-mcp"}}}))
    write(proj / ".claude" / "skills" / "release" / "SKILL.md", "---\ndescription: Cut a release\n---\nsteps\n")
    return home, proj


def apply_plan(inv_path):
    """Dry run, read the plan ID, then apply with it (the approved path)."""
    out = run(SCRIPTS / "apply.py", "--inventory", inv_path)
    pid = out.split("Plan ID: ")[1].split()[0]
    return run(SCRIPTS / "apply.py", "--inventory", inv_path, "--apply", "--plan-id", pid)


def discover(tmp, home, proj):
    out = run(SCRIPTS / "discover.py", "--home", home, "--project", proj, env_home=home)
    inv_path = tmp / "inv.json"
    inv_path.write_text(out)
    return json.loads(out), inv_path


def test_discover_finds_everything_and_skips_secrets(tmp_path):
    home, proj = build_home(tmp_path)
    inv, _ = discover(tmp_path, home, proj)
    assert {s["name"] for s in inv["skills"]} == {"deploy", "release"}
    assert [p["name"] for p in inv["plugins"]] == ["tools"]
    assert inv["plugins"][0]["root"].endswith("1.2.0")          # newest cached version
    assert inv["plugins"][0]["layout"] == "cache"
    assert {m["name"] for m in inv["mcp"]} == {"local", "db", "projsrv"}
    assert inv["left_behind"]["session_transcripts"] == 1
    assert inv["left_behind"]["credential_files_present"] == [".credentials.json"]
    assert "do-not-copy" not in json.dumps(inv)


def test_dry_run_writes_nothing(tmp_path):
    home, proj = build_home(tmp_path)
    _, inv_path = discover(tmp_path, home, proj)
    before = sorted(p for p in tmp_path.rglob("*"))
    out = run(SCRIPTS / "apply.py", "--inventory", inv_path)
    assert "Dry run: nothing written" in out
    assert sorted(p for p in tmp_path.rglob("*")) == before


def test_apply_layout_and_conversions(tmp_path):
    home, proj = build_home(tmp_path)
    _, inv_path = discover(tmp_path, home, proj)
    apply_plan(inv_path)
    cfg = home / ".gemini" / "config" / "plugins"

    manifest = json.loads((cfg / "claude-user" / "plugin.json").read_text())
    assert set(manifest) <= {"$schema", "name", "description"}   # schema: additionalProperties false
    assert (cfg / "claude-user" / "skills" / "deploy" / "scripts" / "go.sh").is_file()
    cmd = (cfg / "claude-user" / "skills" / "fixit" / "SKILL.md").read_text()
    assert "name: fixit" in cmd and "$ARGUMENTS" in cmd

    agent = (cfg / "claude-user" / "agents" / "reviewer.md").read_text()
    assert "- view_file" in agent and "- grep_search" in agent and "- run_command" in agent
    assert "NotebookEdit" not in agent.split("---")[1]               # unknown tool dropped
    assert "model: pro" in agent

    rule = (cfg / "claude-user" / "rules" / "claude-user-memory.md").read_text()
    assert rule.startswith("---\ntrigger: always_on")
    assert "@[Claude Code user memory](" in rule

    user_mcp = json.loads((cfg / "claude-user" / "mcp_config.json").read_text())["mcpServers"]
    assert user_mcp["local"] == {"command": "npx", "args": ["-y", "srv"], "env": {"K": "v"}}

    tools_mcp = json.loads((cfg / "claude-tools" / "mcp_config.json").read_text())["mcpServers"]
    assert tools_mcp["docs"] == {"serverUrl": "https://example.com/mcp"}
    assert (cfg / "claude-tools" / "skills" / "lint" / "SKILL.md").read_text().count("v1.2.0") == 1

    # hooks: proposals only, never live
    assert not list((home / ".gemini").rglob("hooks.json"))
    prop = json.loads((cfg / "claude-tools" / "hooks.proposed.json").read_text())
    entry = next(iter(prop.values()))
    assert entry["enabled"] is False
    assert entry["PostToolUse"][0]["matcher"] == "replace_file_content|write_to_file"
    user_prop = json.loads((cfg / "claude-user" / "hooks.proposed.json").read_text())
    assert any("PreToolUse" in v for v in user_prop.values())
    assert not any("SessionStart" in json.dumps(v) for v in user_prop.values())

    notes = (cfg / "claude-user" / "MIGRATION_NOTES.md").read_text()
    assert "command(npm test)" in notes

    # project
    agents_md = (proj / "AGENTS.md").read_text()
    assert "@[CLAUDE.md](CLAUDE.md)" in agents_md
    pmcp = json.loads((proj / ".agents" / "plugins" / "claude-project" / "mcp_config.json").read_text())["mcpServers"]
    assert set(pmcp) == {"db", "projsrv"}
    assert (proj / ".agents" / "plugins" / "claude-project" / "skills" / "release" / "SKILL.md").is_file()

    # never copied
    assert "do-not-copy" not in "".join(p.read_text(errors="ignore") for p in (home / ".gemini").rglob("*") if p.is_file())
    assert not list((home / ".gemini").rglob("*.jsonl"))


def test_rerun_backs_up_and_is_idempotent(tmp_path):
    home, proj = build_home(tmp_path)
    _, inv_path = discover(tmp_path, home, proj)
    apply_plan(inv_path)
    write(proj / "AGENTS.md", (proj / "AGENTS.md").read_text())  # unchanged content
    apply_plan(inv_path)
    cfg = home / ".gemini" / "config" / "plugins"
    assert list(cfg.glob("claude-user.bak-*"))
    assert (proj / "AGENTS.md").read_text().count("@[CLAUDE.md](CLAUDE.md)") == 1


def test_existing_agy_mcp_name_is_not_overwritten(tmp_path):
    home, proj = build_home(tmp_path)
    write(home / ".gemini" / "config" / "mcp_config.json", json.dumps({"mcpServers": {"local": {"command": "mine"}}}))
    _, inv_path = discover(tmp_path, home, proj)
    out = apply_plan(inv_path)
    assert "MCP local" in out
    user_mcp = home / ".gemini" / "config" / "plugins" / "claude-user" / "mcp_config.json"
    assert not user_mcp.exists() or "local" not in json.loads(user_mcp.read_text())["mcpServers"]


def test_apply_refuses_without_matching_plan_id(tmp_path):
    home, proj = build_home(tmp_path)
    _, inv_path = discover(tmp_path, home, proj)
    res = subprocess.run([sys.executable, str(SCRIPTS / "apply.py"), "--inventory", str(inv_path), "--apply"],
                         capture_output=True, text=True)
    assert res.returncode == 2 and "Refusing to write" in res.stdout
    res = subprocess.run([sys.executable, str(SCRIPTS / "apply.py"), "--inventory", str(inv_path), "--apply",
                          "--plan-id", "deadbeef"], capture_output=True, text=True)
    assert res.returncode == 2
    assert not (home / ".gemini").exists() and not (proj / "AGENTS.md").exists()
