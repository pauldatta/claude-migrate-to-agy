---
name: migrate-from-claude
description: Migrates a Claude Code setup (CLAUDE.md, skills, slash commands, subagents, plugins, MCP servers, hooks, permissions) into Antigravity. Use when the user asks to migrate, import, move or port their Claude Code configuration to Antigravity or agy.
---

# Migrate from Claude Code

You move the user's Claude Code setup into Antigravity in four steps: discover, plan, apply, verify. Nothing is written until the user approves the plan.

The two scripts live in this skill's `scripts/` folder. Resolve that folder's absolute path from this file's location and use it below as `<SKILL_DIR>`. They need only Python 3.9+ and the standard library.

## 1. Discover (read-only)

Ask the user which project folders to include. Default to the current workspace root. Then run:

```bash
python3 <SKILL_DIR>/scripts/discover.py --project <project-root> [--project <another>] > /tmp/claude-inventory.json
```

`discover.py` reads `~/.claude`, `~/.claude.json`, the Claude Desktop config and each project's `CLAUDE.md`, `.claude/` and `.mcp.json`. It never opens credential files or chat transcripts. It only counts them.

If `claude_dir_exists` is false and nothing was found, tell the user and stop.

## 2. Plan (dry run)

```bash
python3 <SKILL_DIR>/scripts/apply.py --inventory /tmp/claude-inventory.json
```

Show the user the three tables from the output in plain language:

- **Moves over**: what will be copied or converted, and where it goes.
- **You check**: items that need a human decision. Common ones:
  - Hooks: Antigravity uses different event names and a different stdin/stdout JSON contract, so hook scripts must be rewritten.
  - Subagent tool lists.
  - MCP servers with env vars that may hold secrets.
  - Permissions.
- **Stays behind**: chat history, credentials, and model settings.

Explain that each source becomes its own Antigravity plugin (`claude-user`, `claude-<plugin>`, `claude-project`), so the user can turn any of them off with `agy plugin disable <name>`.

Then **end your turn**. Never run `--apply` in the same turn as the plan, even if the user asked for both, or asked for "plan only". Wait for a new message from the user that approves the plan. If they want to exclude something, edit `/tmp/claude-inventory.json` to remove it and re-run the dry run.

Pass `--project` only for real project folders. If the workspace is the home directory, skip `--project`, since `~/.claude` is already scanned at user scope.

## 3. Apply

Only after the user approves in a new message. This plugin ships a `PreToolUse` hook that makes Antigravity ask for confirmation (`force_ask`) before any `apply.py --apply` runs, so expect that prompt:

```bash
python3 <SKILL_DIR>/scripts/apply.py --inventory /tmp/claude-inventory.json --apply
```

Existing generated plugins and any `AGENTS.md` the script edits are backed up with a `.bak-<timestamp>` suffix first. Re-running is safe.

Never do any of the following, even if asked to "just make it work":
- copy credential files
- convert chat transcripts
- rename `hooks.proposed.json` to `hooks.json`
- apply permission suggestions

The user does those after review.

## 4. Verify

Tell the user to restart agy (or start a new conversation in Antigravity), then run:

- `/plugin list`: the `claude-*` plugins should appear.
- `/context`: rules and the `AGENTS.md` include should load.
- `/hooks`: only if they activated a reviewed hook.

Report only what those commands actually show. If something didn't load, check:
1. Whether the plugin folder has a valid `plugin.json`.
2. Whether the rule file starts with `trigger:` frontmatter.
3. Whether the workspace is trusted.

## What maps where

| Claude Code | Antigravity |
|:-|:-|
| `CLAUDE.md` in a project | `AGENTS.md` with an inline include, `@[CLAUDE.md](CLAUDE.md)` |
| `~/.claude/CLAUDE.md` | `claude-user/rules/claude-user-memory.md` (`trigger: always_on`, inline include) |
| `~/.claude/skills/<name>/` | `claude-user/skills/<name>/` |
| `~/.claude/commands/<name>.md` | `claude-user/skills/<name>/SKILL.md` |
| `~/.claude/agents/<name>.md` | `claude-user/agents/<name>.md` (tools renamed, unknown tools dropped) |
| Claude plugin | `claude-<plugin>/` with skills, agents, MCP, and hooks as a proposal |
| MCP `url` / `type: http` | `serverUrl` |
| `.mcp.json` in a project | `<project>/.agents/plugins/claude-project/mcp_config.json` |
| hooks in `settings.json` | `hooks.proposed.json` (disabled; rewrite scripts first) |
| `permissions.allow` `Bash(...)` | suggestions in `MIGRATION_NOTES.md` |
