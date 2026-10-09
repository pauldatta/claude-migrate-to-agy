#!/usr/bin/env python3
"""Turn a Claude Code inventory (from discover.py) into Antigravity config.

Dry-run by default: prints the plan and writes nothing. Pass --apply to write.

What it writes (all paths follow the public Antigravity docs):
  ~/.gemini/config/plugins/claude-user/        user skills, commands->skills,
                                               subagents, memory rule, MCP
  ~/.gemini/config/plugins/claude-<plugin>/    one agy plugin per Claude plugin
  <project>/.agents/plugins/claude-project/    project skills, subagents, MCP
  <project>/AGENTS.md                          inline include of CLAUDE.md

Each generated plugin can be switched off as a unit (agy plugin disable <name>).
Hooks and permissions are written as proposals only (MIGRATION_NOTES.md,
hooks.proposed.json); they are never activated automatically.

Usage:
  python3 discover.py --project . > inv.json
  python3 apply.py --inventory inv.json            # dry run
  python3 apply.py --inventory inv.json --apply    # write, with backups
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

# Claude Code tool names -> Antigravity tool names (from the Antigravity hooks doc,
# "Supported tools"). Anything not listed is dropped and reported: the
# subagents doc warns that unknown tool names can hang a subagent.
TOOL_MAP = {
    "Bash": "run_command",
    "Read": "view_file",
    "Write": "write_to_file",
    "Edit": "replace_file_content",
    "MultiEdit": "multi_replace_file_content",
    "Grep": "grep_search",
    "Glob": "find_by_name",
    "LS": "list_dir",
    "WebFetch": "read_url_content",
    "WebSearch": "search_web",
    "Task": "invoke_subagent",
    "Agent": "invoke_subagent",
}

# Claude hook events -> Antigravity hook events (Antigravity hooks doc).
# UserPromptSubmit has no exact match: PreInvocation fires before every model
# call, not only after a user prompt, so it is marked approximate.
HOOK_EVENT_MAP = {
    "PreToolUse": ("PreToolUse", "exact"),
    "PostToolUse": ("PostToolUse", "exact"),
    "Stop": ("Stop", "exact"),
    "UserPromptSubmit": ("PreInvocation", "approximate"),
}

# Claude model aliases -> Antigravity subagent model tiers (subagents doc).
MODEL_MAP = {"haiku": "flash", "opus": "pro"}

NAME_OK = re.compile(r"[^a-zA-Z0-9_-]+")


def slug(text: str) -> str:
    return NAME_OK.sub("-", text).strip("-").lower() or "unnamed"


def yaml_scalar(val: str) -> str:
    val = str(val)
    if val == "" or any(c in val for c in ":#{}[],&*!|>'\"%@`") or val != val.strip():
        return json.dumps(val)  # JSON strings are valid YAML scalars
    return val


def frontmatter(meta: dict) -> str:
    lines = ["---"]
    for key, val in meta.items():
        if isinstance(val, list):
            lines.append(f"{key}:")
            lines += [f"  - {yaml_scalar(v)}" for v in val]
        elif isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        else:
            lines.append(f"{key}: {yaml_scalar(val)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def body_of(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def first_line(text: str, limit: int = 160) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:limit]
    return ""


# ---------------------------------------------------------------- conversions
def convert_mcp(cfg: dict) -> tuple[dict, list[str]]:
    """Claude MCP server entry -> Antigravity mcp_config.json entry."""
    notes = []
    out: dict = {}
    if cfg.get("url") or cfg.get("type") in ("http", "sse"):
        url = cfg.get("url") or cfg.get("serverUrl") or cfg.get("httpUrl")
        if not url:
            return {}, ["remote server without a URL; skipped"]
        out["serverUrl"] = url
        if cfg.get("headers"):
            out["headers"] = cfg["headers"]
            notes.append("has headers: confirm your Antigravity version honors them")
    elif cfg.get("command"):
        out["command"] = cfg["command"]
        if cfg.get("args"):
            out["args"] = cfg["args"]
        if cfg.get("cwd"):
            out["cwd"] = cfg["cwd"]
    else:
        return {}, ["neither command nor url; skipped"]
    if cfg.get("env"):
        out["env"] = cfg["env"]
        notes.append(f"{len(cfg['env'])} env var(s) copied as-is: review for secrets")
    return out, notes


def convert_agent(entry: dict) -> tuple[str, list[str]]:
    meta = entry.get("meta", {})
    notes = []
    name = slug(meta.get("name") or Path(entry["path"]).stem)
    out = {"name": name,
           "description": meta.get("description") or first_line(body_of(entry["path"])) or name,
           "subagent": True}
    tools = meta.get("tools")
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",") if t.strip()]
    if tools:
        mapped, dropped = [], []
        for tool in tools:
            base = tool.split("(")[0].strip()
            target = TOOL_MAP.get(base)
            if target and target not in mapped:
                mapped.append(target)
            elif not target:
                dropped.append(tool)
        if mapped:
            out["tools"] = mapped
        if dropped:
            notes.append(f"dropped tools with no Antigravity equivalent: {', '.join(dropped)}")
    else:
        notes.append("no tools list in the Claude agent: check which tools it needs")
    model = str(meta.get("model", "")).lower()
    if model:
        out["model"] = MODEL_MAP.get(model, "inherit")
        if model not in MODEL_MAP:
            notes.append(f"model '{model}' set to inherit")
    return frontmatter(out) + "\n" + body_of(entry["path"]), notes


def convert_command(entry: dict, name: str) -> str:
    meta = entry.get("meta", {})
    body = body_of(entry["path"])
    desc = meta.get("description") or first_line(body) or f"Converted Claude Code command /{name}"
    note = ""
    if "$ARGUMENTS" in body or re.search(r"\$\d", body):
        note = ("> Converted from a Claude Code slash command. `$ARGUMENTS` (and `$1`, `$2`, ...) "
                "mean the text the user typed after the command.\n\n")
    return frontmatter({"name": name, "description": desc}) + "\n" + note + body


def convert_hooks(hooks: dict, origin: str) -> tuple[dict, list[str]]:
    """Claude hooks object -> Antigravity hooks.json shape (as a proposal)."""
    proposal, notes = {}, []
    for event, groups in (hooks or {}).items():
        if event not in HOOK_EVENT_MAP:
            notes.append(f"{origin}: {event} has no Antigravity event; not converted")
            continue
        target, quality = HOOK_EVENT_MAP[event]
        handlers_out = []
        for group in groups if isinstance(groups, list) else []:
            matcher = group.get("matcher", "")
            parts = [p for p in re.split(r"\|", matcher) if p] if matcher else []
            mapped = [TOOL_MAP.get(p, p) for p in parts]
            unknown = [p for p in parts if p not in TOOL_MAP and p not in ("*", ".*")]
            handlers = [{"type": "command", "command": h.get("command", ""),
                         **({"timeout": h["timeout"]} if "timeout" in h else {})}
                        for h in group.get("hooks", []) if h.get("type", "command") == "command"]
            if target in ("PreToolUse", "PostToolUse"):
                handlers_out.append({"matcher": "|".join(mapped), "hooks": handlers})
            else:
                handlers_out += handlers
            if unknown:
                notes.append(f"{origin}: matcher names kept unmapped: {', '.join(unknown)}")
        name = slug(f"{origin}-{event}")
        proposal[name] = {"enabled": False, target: handlers_out}
        if quality == "approximate":
            notes.append(f"{origin}: {event} mapped to {target}, which fires before every model call")
    if proposal:
        notes.append(f"{origin}: hook scripts need rewriting. Antigravity sends JSON with toolCall.name/args "
                     "on stdin and reads a JSON decision on stdout")
    return proposal, notes


# ---------------------------------------------------------------- planning
class Plan:
    def __init__(self):
        self.files: dict[Path, str] = {}      # dest -> text content
        self.trees: dict[Path, Path] = {}     # dest dir -> source dir
        self.appends: dict[Path, str] = {}    # existing file -> text to append
        self.moves: list[tuple[str, str, str]] = []   # (what, from, to)
        self.checks: list[tuple[str, str]] = []       # (what, why)
        self.left: list[tuple[str, str]] = []         # (what, why)

    def plugin(self, root: Path, name: str, description: str):
        self.files[root / "plugin.json"] = json.dumps(
            {"$schema": "https://antigravity.google/schemas/v1/plugin.json",
             "name": name, "description": description}, indent=2) + "\n"


def existing_mcp_names(gemini_config: Path) -> set[str]:
    try:
        data = json.loads((gemini_config / "mcp_config.json").read_text(encoding="utf-8-sig"))
        return set((data.get("mcpServers") or {}).keys())
    except (OSError, ValueError):
        return set()


def add_skills(plan: Plan, root: Path, skills: list[dict], used: set[str], prefix: str = ""):
    for sk in skills:
        folder = slug(sk["name"])
        if folder in used:
            folder = slug(f"{prefix}{folder}") if prefix else f"{folder}-2"
        used.add(folder)
        plan.trees[root / "skills" / folder] = Path(sk["dir"])
        plan.moves.append(("skill", sk["dir"], str(root / "skills" / folder)))
        if sk.get("meta", {}).get("allowed-tools"):
            plan.checks.append((f"skill {folder}", "has Claude `allowed-tools`; Antigravity skills only read name and description, so that list is ignored"))


def add_commands(plan: Plan, root: Path, commands: list[dict], used: set[str]):
    for cmd in commands:
        name = slug(Path(cmd["path"]).stem)
        folder = name if name not in used else f"cmd-{name}"
        used.add(folder)
        plan.files[root / "skills" / folder / "SKILL.md"] = convert_command(cmd, folder)
        plan.moves.append(("command -> skill", cmd["path"], str(root / "skills" / folder)))


def add_agents(plan: Plan, root: Path, agents: list[dict]):
    for ag in agents:
        text, notes = convert_agent(ag)
        name = slug(ag.get("meta", {}).get("name") or Path(ag["path"]).stem)
        plan.files[root / "agents" / f"{name}.md"] = text
        plan.moves.append(("subagent", ag["path"], str(root / "agents" / f"{name}.md")))
        for n in notes:
            plan.checks.append((f"subagent {name}", n))


def add_mcp(plan: Plan, root: Path, servers: list[dict], taken: set[str]):
    out = {}
    for srv in servers:
        name = srv["name"]
        if name in taken:
            plan.checks.append((f"MCP {name}", f"already defined elsewhere; kept the existing one (from {srv['source']} skipped)"))
            continue
        conv, notes = convert_mcp(srv["config"])
        if not conv:
            plan.checks.append((f"MCP {name}", "; ".join(notes)))
            continue
        out[name] = conv
        taken.add(name)
        plan.moves.append(("MCP server", f"{srv['source']}:{name}", str(root / "mcp_config.json")))
        for n in notes:
            plan.checks.append((f"MCP {name}", n))
    if out:
        plan.files[root / "mcp_config.json"] = json.dumps({"mcpServers": out}, indent=2) + "\n"


def build_plan(inv: dict, gemini_config: Path) -> Plan:
    plan = Plan()
    taken_mcp = existing_mcp_names(gemini_config)
    projects = [Path(p) for p in inv.get("projects", [])]
    proj_scopes = {f"project:{p}": p for p in projects}

    # ---- user-level plugin
    user_root = gemini_config / "plugins" / "claude-user"
    user_skills = [s for s in inv["skills"] if s["scope"] == "user"]
    user_cmds = [c for c in inv["commands"] if c["scope"] == "user"]
    user_agents = [a for a in inv["agents"] if a["scope"] == "user"]
    user_mcp = [m for m in inv["mcp"] if not m["source"].endswith(".mcp.json") and "projects[" not in m["source"]]
    user_mem = [i for i in inv["instructions"] if i["scope"] == "user"]
    if user_skills or user_cmds or user_agents or user_mcp or user_mem:
        plan.plugin(user_root, "claude-user", "Claude Code user-level skills, commands, subagents and MCP servers, migrated by claude-migrate-to-agy.")
        used: set[str] = set()
        add_skills(plan, user_root, user_skills, used)
        add_commands(plan, user_root, user_cmds, used)
        add_agents(plan, user_root, user_agents)
        add_mcp(plan, user_root, user_mcp, taken_mcp)
        for mem in user_mem:
            # Inline include keeps a single source of truth (rules doc: @[label](path), ~/ expands).
            rule = frontmatter({"trigger": "always_on", "description": "Claude Code user memory (~/.claude/CLAUDE.md)"}) \
                + "\n@[Claude Code user memory](" + mem["path"] + ")\n"
            plan.files[user_root / "rules" / "claude-user-memory.md"] = rule
            plan.moves.append(("user memory", mem["path"], str(user_root / "rules" / "claude-user-memory.md")))
            if mem.get("chars", 0) > 12000:
                plan.checks.append(("user memory", f"{mem['chars']} characters; large rules count against the rule budget, consider splitting"))

    # ---- one plugin per Claude plugin
    for pl in inv.get("plugins", []):
        name = f"claude-{slug(pl['name'])}"
        root = gemini_config / "plugins" / name
        plan.plugin(root, name, f"Migrated from Claude Code plugin '{pl['name']}' by claude-migrate-to-agy.")
        used = set()
        add_skills(plan, root, pl["skills"], used)
        add_commands(plan, root, pl["commands"], used)
        add_agents(plan, root, pl["agents"])
        add_mcp(plan, root, pl["mcp"], taken_mcp)
        if pl.get("hooks"):
            proposal, notes = convert_hooks(pl["hooks"], name)
            plan.files[root / "hooks.proposed.json"] = json.dumps(proposal, indent=2) + "\n"
            plan.checks.append((f"hooks in {name}", "written to hooks.proposed.json (not loaded). Rename to hooks.json after review"))
            plan.checks += [(f"hooks in {name}", n) for n in notes]

    for sk in inv.get("plugins_skipped", []):
        plan.checks.append((f"plugin {sk['plugin']}", f"not migrated: {sk['reason']}"))

    # ---- per project
    for scope, proj in proj_scopes.items():
        root = proj / ".agents" / "plugins" / "claude-project"
        p_skills = [s for s in inv["skills"] if s["scope"] == scope]
        p_cmds = [c for c in inv["commands"] if c["scope"] == scope]
        p_agents = [a for a in inv["agents"] if a["scope"] == scope]
        p_mcp = [m for m in inv["mcp"] if m["source"] == str(proj / ".mcp.json") or f"projects[{proj}]" in m["source"]]
        if p_skills or p_cmds or p_agents or p_mcp:
            plan.plugin(root, "claude-project", f"Claude Code project assets for {proj.name}, migrated by claude-migrate-to-agy.")
            used = set()
            add_skills(plan, root, p_skills, used)
            add_commands(plan, root, p_cmds, used)
            add_agents(plan, root, p_agents)
            add_mcp(plan, root, p_mcp, set(taken_mcp))
        for ins in [i for i in inv["instructions"] if i["scope"] == scope]:
            rel = Path(ins["path"]).relative_to(proj).as_posix()
            include = f"@[{rel}]({rel})"
            agents_md = proj / "AGENTS.md"
            if agents_md.is_file():
                current = agents_md.read_text(encoding="utf-8")
                if include in current or f"({rel})" in current:
                    continue
                plan.appends[agents_md] = f"\n<!-- Added by claude-migrate-to-agy: inline {rel} -->\n{include}\n"
            else:
                plan.files[agents_md] = f"<!-- Added by claude-migrate-to-agy: inline {rel} -->\n{include}\n"
            plan.moves.append(("instructions", ins["path"], f"{agents_md} (inline include)"))

    # ---- settings hooks and permissions: proposals only
    notes_lines = []
    for hk in inv.get("hooks", []):
        proposal, notes = convert_hooks(hk["hooks"], slug(hk["scope"]))
        target = (gemini_config / "plugins" / "claude-user") if hk["scope"] == "user" else \
            (proj_scopes.get(hk["scope"], Path(hk["source"]).parent.parent) / ".agents" / "plugins" / "claude-project")
        if not (target / "plugin.json") in plan.files:
            plan.plugin(target, target.name, "Proposals migrated by claude-migrate-to-agy.")
        plan.files[target / "hooks.proposed.json"] = json.dumps(proposal, indent=2) + "\n"
        plan.checks.append((f"hooks ({hk['scope']})", f"written to {target / 'hooks.proposed.json'} (not loaded)"))
        plan.checks += [(f"hooks ({hk['scope']})", n) for n in notes]
    for perm in inv.get("permissions", []):
        bash = [a for a in perm["allow"] if a.startswith("Bash(")]
        other = [a for a in perm["allow"] if not a.startswith("Bash(")]
        if bash:
            notes_lines.append(f"## Command allowlist from {perm['source']}\n")
            notes_lines += [f"- `{a}` -> consider `command({a[5:-1]})`" for a in bash]
            notes_lines.append("")
        if other:
            plan.checks.append((f"permissions ({perm['scope']})", f"{len(other)} non-shell grants have no direct equivalent"))
        plan.checks.append((f"permissions ({perm['scope']})", "listed in MIGRATION_NOTES.md as suggestions; nothing applied"))
    if notes_lines:
        plan.files[gemini_config / "plugins" / "claude-user" / "MIGRATION_NOTES.md"] = \
            "# Permission suggestions (not applied)\n\nReview these against your Antigravity permission settings.\n\n" + "\n".join(notes_lines)
        if (gemini_config / "plugins" / "claude-user" / "plugin.json") not in plan.files:
            plan.plugin(gemini_config / "plugins" / "claude-user", "claude-user", "Proposals migrated by claude-migrate-to-agy.")

    # ---- left behind
    lb = inv.get("left_behind", {})
    if lb.get("session_transcripts"):
        plan.left.append((f"{lb['session_transcripts']} chat session(s)", "Antigravity stores conversations in a different, internal format"))
    if lb.get("credential_files_present"):
        plan.left.append(("credentials", "never copied; sign in to Antigravity separately"))
    else:
        plan.left.append(("logins and API keys", "never copied; sign in to Antigravity separately"))
    if lb.get("settings"):
        plan.left.append(("model / effort / env settings", ", ".join(lb["settings"])))
    return plan


# ---------------------------------------------------------------- output / apply
def render(plan: Plan) -> str:
    out = ["# Claude Code -> Antigravity migration plan", ""]
    out += ["## Moves over", "", "| What | From | To |", "|:-|:-|:-|"]
    out += [f"| {w} | `{a}` | `{b}` |" for w, a, b in plan.moves] or ["| (nothing found) | | |"]
    out += ["", "## You check", "", "| Item | Why |", "|:-|:-|"]
    out += [f"| {w} | {y} |" for w, y in plan.checks] or ["| (nothing) | |"]
    out += ["", "## Stays behind", "", "| Item | Why |", "|:-|:-|"]
    out += [f"| {w} | {y} |" for w, y in plan.left]
    writes = sorted({str(p) for p in list(plan.files) + list(plan.trees)} | {f"{p} (append)" for p in plan.appends})
    out += ["", f"## Files to write ({len(writes)})", ""] + [f"- `{w}`" for w in writes]
    return "\n".join(out) + "\n"


def backup(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    if path.is_dir():
        shutil.copytree(path, dest, symlinks=True)
    else:
        shutil.copy2(path, dest)
    return dest


def apply(plan: Plan) -> list[str]:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = []
    plugin_roots = {p.parent for p in plan.files if p.name == "plugin.json"}
    for root in sorted(plugin_roots):
        b = backup(root, stamp)
        if b:
            shutil.rmtree(root)
            log.append(f"backed up {root} -> {b}")
    for dest, src in plan.trees.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
        log.append(f"copied {src} -> {dest}")
    for dest, text in plan.files.items():
        if dest.exists() and dest.parent not in plugin_roots and not any(r in dest.parents for r in plugin_roots):
            b = backup(dest, stamp)
            log.append(f"backed up {dest} -> {b}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        log.append(f"wrote {dest}")
    for dest, text in plan.appends.items():
        b = backup(dest, stamp)
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(text)
        log.append(f"appended to {dest} (backup {b})")
    return log


def plan_id(plan: Plan) -> str:
    """Short fingerprint of exactly what would be written. --apply must quote it."""
    h = hashlib.sha256(render(plan).encode("utf-8"))
    for dest in sorted(map(str, plan.files)):
        h.update(dest.encode() + b"\0" + plan.files[Path(dest)].encode())
    return h.hexdigest()[:8]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", required=True, help="JSON from discover.py ('-' for stdin)")
    ap.add_argument("--gemini-config", help="Antigravity config dir (default: <home>/.gemini/config)")
    ap.add_argument("--apply", action="store_true", help="write files (default: dry run)")
    ap.add_argument("--plan-id", help="plan ID printed by the dry run; required with --apply")
    args = ap.parse_args(argv)
    inv = json.load(sys.stdin if args.inventory == "-" else open(args.inventory, encoding="utf-8"))
    gemini_config = Path(args.gemini_config) if args.gemini_config else Path(inv["home"]) / ".gemini" / "config"
    plan = build_plan(inv, gemini_config)
    pid = plan_id(plan)
    sys.stdout.write(render(plan))
    sys.stdout.write(f"\nPlan ID: {pid}\n")
    if not args.apply:
        sys.stdout.write("\nDry run: nothing written. After the user approves this plan, re-run with "
                         f"--apply --plan-id {pid}\n")
        return 0
    if args.plan_id != pid:
        sys.stdout.write(f"\nRefusing to write: --plan-id {args.plan_id!r} does not match this plan ({pid}). "
                         "Show the user the current plan and get approval first.\n")
        return 2
    sys.stdout.write("\n## Applied\n\n" + "\n".join(f"- {line}" for line in apply(plan)) + "\n")
    sys.stdout.write("\nNext: restart agy, then run /plugin list and /context to confirm what loaded.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
