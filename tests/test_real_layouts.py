"""Layouts seen on a real Claude Code 2.1.x install (validated on a Cloud Workstation).

- enabledPlugins in settings.json, plugin loaded in place from the marketplace clone
- remote-source marketplace entries that are not on disk
- user skills that are symlinks into another folder, with Claude `allowed-tools`
"""
import json
import os

from test_migrate import SCRIPTS, apply_plan, run, write


def test_marketplace_in_place_plugins_and_symlinked_skills(tmp_path):
    home, proj = tmp_path / "home", tmp_path / "work" / "app"
    proj.mkdir(parents=True)
    c = home / ".claude"
    mp = c / "plugins" / "marketplaces" / "official"
    write(c / "plugins" / "known_marketplaces.json", json.dumps(
        {"official": {"source": {"source": "github", "repo": "x/y"}, "installLocation": str(mp)}}))
    write(mp / ".claude-plugin" / "marketplace.json", json.dumps({"name": "official", "plugins": [
        {"name": "frontend-design", "source": "./plugins/frontend-design"},
        {"name": "remote-one", "source": {"source": "git-subdir", "url": "https://example.com/r.git"}},
        {"name": "not-enabled", "source": "./plugins/not-enabled"}]}))
    write(mp / "plugins" / "frontend-design" / ".claude-plugin" / "plugin.json", json.dumps({"name": "frontend-design"}))
    write(mp / "plugins" / "frontend-design" / "skills" / "frontend-design" / "SKILL.md",
          "---\nname: frontend-design\ndescription: Build UIs.\n---\nbody\n")
    write(mp / "plugins" / "not-enabled" / ".claude-plugin" / "plugin.json", json.dumps({"name": "not-enabled"}))
    write(c / "settings.json", json.dumps({"enabledPlugins": {
        "frontend-design@official": True, "remote-one@official": True, "old@official": False}}))
    real = home / ".agents" / "skills" / "linked"
    write(real / "SKILL.md", "---\nname: linked\ndescription: Linked skill.\nallowed-tools: Read, Bash\n---\nhi\n")
    (c / "skills").mkdir(parents=True)
    os.symlink("../../.agents/skills/linked", c / "skills" / "linked")

    inv_path = tmp_path / "inv.json"
    inv_path.write_text(run(SCRIPTS / "discover.py", "--home", home, "--project", proj, env_home=home))
    inv = json.loads(inv_path.read_text())
    assert [p["name"] for p in inv["plugins"]] == ["frontend-design"]
    assert inv["plugins"][0]["layout"] == "marketplace"
    reasons = {s["plugin"]: s["reason"] for s in inv["plugins_skipped"]}
    assert "remote source" in reasons["remote-one@official"]
    assert reasons["old@official"] == "disabled in Claude Code"
    assert "not-enabled" not in json.dumps(inv["plugins"])
    assert [s["name"] for s in inv["skills"]] == ["linked"]

    out = apply_plan(inv_path)
    cfg = home / ".gemini" / "config" / "plugins"
    assert (cfg / "claude-frontend-design" / "skills" / "frontend-design" / "SKILL.md").is_file()
    linked = cfg / "claude-user" / "skills" / "linked"
    assert linked.is_dir() and not linked.is_symlink() and (linked / "SKILL.md").is_file()
    assert "allowed-tools" in out and "remote-one@official" in out


def test_project_at_home_is_not_double_counted(tmp_path):
    home = tmp_path / "home"
    write(home / ".claude" / "skills" / "one" / "SKILL.md", "---\ndescription: One.\n---\nx\n")
    write(home / ".claude" / "settings.local.json", json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    inv = json.loads(run(SCRIPTS / "discover.py", "--home", home, "--project", home, env_home=home))
    assert [s["scope"] for s in inv["skills"]] == ["user"]
    assert [p["scope"] for p in inv["permissions"]] == ["user"]
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv))
    apply_plan(inv_path)
    assert not (home / ".agents" / "plugins" / "claude-project").exists()

