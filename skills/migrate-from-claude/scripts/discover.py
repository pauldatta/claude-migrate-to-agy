#!/usr/bin/env python3
"""Read-only inventory of a Claude Code setup, for migration to Antigravity.

Prints JSON describing what was found. Never writes files, never reads
credentials, never opens session transcripts (it only counts them).

Usage:
  python3 discover.py [--home DIR] [--project DIR ...] [--pretty]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Files that may hold secrets. We never open them.
CREDENTIAL_NAMES = {".credentials.json", "credentials.json", "auth.json"}


# ---------------------------------------------------------------- helpers
def read_json(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-frontmatter reader (flat keys, scalar or list values).

    Kept dependency-free on purpose; unsupported constructs are returned as
    raw strings rather than guessed at.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta: dict = {}
    key = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and key and line.strip().startswith("- "):
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(line.strip()[2:].strip().strip("'\""))
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                meta[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            elif val == "":
                meta[key] = []
            else:
                meta[key] = val.strip("'\"")
    return meta, body


def md_entry(path: Path, kind: str, scope: str) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"kind": kind, "scope": scope, "path": str(path), "error": str(exc)}
    meta, body = split_frontmatter(text)
    return {"kind": kind, "scope": scope, "path": str(path), "meta": meta,
            "chars": len(text), "body_chars": len(body)}


def list_md(dirpath: Path, kind: str, scope: str) -> list[dict]:
    if not dirpath.is_dir():
        return []
    return [md_entry(p, kind, scope) for p in sorted(dirpath.glob("*.md")) if p.is_file()]


def list_skills(dirpath: Path, scope: str) -> list[dict]:
    out = []
    if not dirpath.is_dir():
        return out
    for skill_md in sorted(dirpath.glob("*/SKILL.md")):
        entry = md_entry(skill_md, "skill", scope)
        entry["dir"] = str(skill_md.parent)
        entry["name"] = entry.get("meta", {}).get("name") or skill_md.parent.name
        out.append(entry)
    return out


def mcp_servers_from(data: dict | None, source: str) -> list[dict]:
    out = []
    if not data:
        return out
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        for name, cfg in servers.items():
            if isinstance(cfg, dict):
                out.append({"name": name, "source": source, "config": cfg})
    return out


def version_key(path: Path) -> tuple:
    parts = []
    for piece in path.name.replace("-", ".").split("."):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


# ---------------------------------------------------------------- discovery
def plugin_record(name: str, root: Path, layout: str, marketplace: str | None) -> dict:
    hooks_file = root / "hooks" / "hooks.json"
    return {
        "name": name,
        "marketplace": marketplace,
        "root": str(root),
        "layout": layout,
        "skills": list_skills(root / "skills", f"plugin:{name}"),
        "commands": list_md(root / "commands", "command", f"plugin:{name}"),
        "agents": list_md(root / "agents", "agent", f"plugin:{name}"),
        "mcp": mcp_servers_from(read_json(root / ".mcp.json"), f"plugin:{name}"),
        "hooks": (read_json(hooks_file) or {}).get("hooks", {}) if hooks_file.is_file() else {},
        "hooks_dir": str(root / "hooks"),
    }


def resolve_from_marketplace(plugins_root: Path, name: str, marketplace: str) -> tuple[Path | None, str]:
    """Locate an enabled plugin inside its marketplace clone.

    Current Claude Code loads marketplace plugins in place:
    known_marketplaces.json -> installLocation -> .claude-plugin/marketplace.json
    -> plugins[].source (a relative path for plugins bundled in the marketplace).
    """
    known = read_json(plugins_root / "known_marketplaces.json") or {}
    loc = Path((known.get(marketplace) or {}).get("installLocation") or plugins_root / "marketplaces" / marketplace)
    manifest = read_json(loc / ".claude-plugin" / "marketplace.json") or {}
    for entry in manifest.get("plugins") or []:
        if not isinstance(entry, dict) or entry.get("name") != name:
            continue
        src = entry.get("source")
        if isinstance(src, str):
            root = (loc / src).resolve()
            return (root, "marketplace") if root.is_dir() else (None, f"source {src} not found in {loc}")
        kind = src.get("source", "unknown") if isinstance(src, dict) else "unknown"
        return None, f"remote source ({kind}) is not on disk"
    for guess in (loc / "plugins" / name, loc / "external_plugins" / name):
        if guess.is_dir():
            return guess, "marketplace"
    return None, f"not found in marketplace {marketplace}"


def find_plugins(plugins_root: Path, enabled: dict | None) -> tuple[list[dict], list[dict]]:
    """Find Claude plugins. Returns (plugins to migrate, skipped plugins with reasons).

    Layouts handled:
      flat         plugins/<name>/.claude-plugin/plugin.json
      cache        plugins/cache/<marketplace>/<name>/<version>/.claude-plugin/plugin.json
      marketplace  plugins/marketplaces/<marketplace>/<source>/ (loaded in place)
    When settings.json has `enabledPlugins`, only entries set to true migrate.
    """
    if not plugins_root.is_dir():
        return [], []
    installed: dict[str, tuple[Path, str, str | None]] = {}
    for manifest in plugins_root.glob("*/.claude-plugin/plugin.json"):
        root = manifest.parent.parent
        if root.name in ("cache", "marketplaces"):
            continue
        name = (read_json(manifest) or {}).get("name") or root.name
        installed[name] = (root, "flat", None)
    cached: dict[str, list[tuple[Path, str]]] = {}
    for manifest in plugins_root.glob("cache/*/*/*/.claude-plugin/plugin.json"):
        root = manifest.parent.parent
        cached.setdefault(root.parent.name, []).append((root, root.parent.parent.name))
    for name, roots in cached.items():
        root, mp = sorted(roots, key=lambda r: version_key(r[0]))[-1]
        installed[name] = (root, "cache", mp)

    plugins, skipped = [], []
    if enabled is None:  # older installs: no enabledPlugins map, take what is installed
        for name, (root, layout, mp) in sorted(installed.items()):
            plugins.append(plugin_record(name, root, layout, mp))
        return plugins, skipped
    for key, on in sorted(enabled.items()):
        name, _, mp = key.partition("@")
        if not on:
            skipped.append({"plugin": key, "reason": "disabled in Claude Code"})
            continue
        if name in installed:
            root, layout, cmp = installed.pop(name)
            plugins.append(plugin_record(name, root, layout, cmp or mp or None))
            continue
        root, why = resolve_from_marketplace(plugins_root, name, mp) if mp else (None, "no marketplace in key")
        if root:
            plugins.append(plugin_record(name, root, why, mp))
        else:
            skipped.append({"plugin": key, "reason": why})
    for name in sorted(installed):
        skipped.append({"plugin": name, "reason": "installed but not listed in enabledPlugins"})
    return plugins, skipped


def desktop_config_paths(home: Path) -> list[Path]:
    return [
        home / "Library/Application Support/Claude/claude_desktop_config.json",
        home / ".config/Claude/claude_desktop_config.json",
        Path(os.environ.get("APPDATA", str(home / "AppData/Roaming"))) / "Claude/claude_desktop_config.json",
    ]


def discover(home: Path, projects: list[Path]) -> dict:
    claude_dir = Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") else home / ".claude"
    state_file = claude_dir / ".claude.json" if os.environ.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    state = read_json(state_file) or {}

    inv: dict = {
        "home": str(home), "claude_dir": str(claude_dir),
        "claude_dir_exists": claude_dir.is_dir(),
        "instructions": [], "skills": [], "commands": [], "agents": [],
        "plugins": [], "plugins_skipped": [], "mcp": [], "hooks": [], "permissions": [],
        "left_behind": {}, "projects": [str(p) for p in projects],
    }

    # user-level
    user_md = claude_dir / "CLAUDE.md"
    if user_md.is_file():
        inv["instructions"].append(md_entry(user_md, "instructions", "user"))
    inv["skills"] += list_skills(claude_dir / "skills", "user")
    inv["commands"] += list_md(claude_dir / "commands", "command", "user")
    inv["agents"] += list_md(claude_dir / "agents", "agent", "user")
    enabled = None
    for name in ("settings.json", "settings.local.json"):
        ep = (read_json(claude_dir / name) or {}).get("enabledPlugins")
        if isinstance(ep, dict):
            enabled = {**(enabled or {}), **ep}
    inv["plugins"], inv["plugins_skipped"] = find_plugins(claude_dir / "plugins", enabled)

    for name in ("settings.json", "settings.local.json"):
        data = read_json(claude_dir / name) or {}
        if data.get("hooks"):
            inv["hooks"].append({"scope": "user", "source": str(claude_dir / name), "hooks": data["hooks"]})
        allow = (data.get("permissions") or {}).get("allow") or []
        if allow:
            inv["permissions"].append({"scope": "user", "source": str(claude_dir / name), "allow": allow})
        for key in ("model", "effortLevel", "env"):
            if key in data:
                inv["left_behind"].setdefault("settings", []).append(f"{name}:{key}")

    # MCP: global state file, desktop app, and a legacy user file if present
    inv["mcp"] += mcp_servers_from(state, f"{state_file}")
    inv["mcp"] += mcp_servers_from(read_json(claude_dir / "mcp.json"), str(claude_dir / "mcp.json"))
    for cfg in desktop_config_paths(home):
        if cfg.is_file():
            inv["mcp"] += mcp_servers_from(read_json(cfg), str(cfg))

    # project-level
    proj_state = state.get("projects") if isinstance(state.get("projects"), dict) else {}
    for proj in projects:
        scope = f"project:{proj}"
        for md in (proj / "CLAUDE.md", proj / ".claude" / "CLAUDE.md"):
            if md.is_file():
                entry = md_entry(md, "instructions", scope)
                entry["project"] = str(proj)
                entry["has_agents_md"] = (proj / "AGENTS.md").is_file()
                inv["instructions"].append(entry)
        # A project at ~ has ~/.claude as its ".claude": that is the user-level
        # dir, already scanned above, so don't count it twice.
        same_as_user = (proj / ".claude").resolve() == claude_dir.resolve()
        if same_as_user:
            inv.setdefault("notes", []).append(f"{proj}: .claude is the user-level dir; scanned once as user scope")
        else:
            inv["skills"] += list_skills(proj / ".claude" / "skills", scope)
            inv["commands"] += list_md(proj / ".claude" / "commands", "command", scope)
            inv["agents"] += list_md(proj / ".claude" / "agents", "agent", scope)
        inv["mcp"] += mcp_servers_from(read_json(proj / ".mcp.json"), str(proj / ".mcp.json"))
        entry = proj_state.get(str(proj)) or proj_state.get(proj.as_posix())
        if isinstance(entry, dict):
            inv["mcp"] += mcp_servers_from(entry, f"{state_file}:projects[{proj}]")
        for name in ("settings.json", "settings.local.json") if not same_as_user else ():
            data = read_json(proj / ".claude" / name) or {}
            if data.get("hooks"):
                inv["hooks"].append({"scope": scope, "source": str(proj / ".claude" / name), "hooks": data["hooks"]})
            allow = (data.get("permissions") or {}).get("allow") or []
            if allow:
                inv["permissions"].append({"scope": scope, "source": str(proj / ".claude" / name), "allow": allow})

    # things that cannot move: count only, never open
    sessions = list((claude_dir / "projects").glob("*/*.jsonl")) if (claude_dir / "projects").is_dir() else []
    inv["left_behind"]["session_transcripts"] = len(sessions)
    inv["left_behind"]["credential_files_present"] = sorted(
        p.name for p in claude_dir.iterdir() if p.name in CREDENTIAL_NAMES) if claude_dir.is_dir() else []
    return inv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=str(Path.home()), help="home directory to scan (default: $HOME)")
    ap.add_argument("--project", action="append", default=[], help="project root to scan (repeatable; default: cwd)")
    ap.add_argument("--pretty", action="store_true", help="indent JSON output")
    args = ap.parse_args(argv)
    projects = [Path(p).resolve() for p in (args.project or [os.getcwd()])]
    inv = discover(Path(args.home).expanduser().resolve(), projects)
    json.dump(inv, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
