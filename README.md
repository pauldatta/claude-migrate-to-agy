# claude-migrate-to-agy

An [Antigravity](https://antigravity.google) plugin that moves your Claude Code setup into Antigravity: skills, slash commands, subagents, plugins, MCP servers, and CLAUDE.md instructions. It shows you a plan first and writes nothing until you approve it.

> [!NOTE]
> This is not an officially supported Google product.

## Install

```bash
agy plugin install https://github.com/pauldatta/claude-migrate-to-agy
```

Then, in agy or the Antigravity app:

```
migrate my Claude setup
```

The `migrate-from-claude` skill takes it from there.

## What happens

1. **Discover (read-only).** Scans `~/.claude`, `~/.claude.json`, the Claude Desktop config, and the projects you name. It never opens credential files or chat transcripts. It only counts them.
2. **Plan.** Shows three tables: what moves over, what you need to check, and what stays behind.
3. **Apply (after you say yes).** The dry run prints a plan ID, and `--apply` refuses to write unless it gets that ID. agy also asks before running the command. It writes Antigravity plugins and backs up anything it replaces. Running it again is safe.
4. **Verify.** You run `/plugin list` and `/context` in agy to confirm what loaded.

Every source becomes its own plugin, so you can switch any of them off with `agy plugin disable <name>`:

| Generated plugin | Location | Contains |
|:-|:-|:-|
| `claude-user` | `~/.gemini/config/plugins/` | your user skills, commands (as skills), subagents, MCP servers, and an always-on rule that inlines `~/.claude/CLAUDE.md` |
| `claude-<plugin>` | `~/.gemini/config/plugins/` | one per enabled Claude Code plugin |
| `claude-project` | `<project>/.agents/plugins/` | project skills, subagents, `.mcp.json` servers |

Project `CLAUDE.md` files are inlined from `AGENTS.md` with `@[CLAUDE.md](CLAUDE.md)`. Claude Code keeps reading the original file, so you don't maintain two copies.

## What maps where

| Claude Code | Antigravity | Notes |
|:-|:-|:-|
| `~/.claude/skills/<name>/` | `claude-user/skills/<name>/` | copied, symlinks resolved; `allowed-tools` is flagged because Antigravity ignores it |
| `~/.claude/commands/<name>.md` | `claude-user/skills/<name>/SKILL.md` | `$ARGUMENTS` explained in the skill |
| `~/.claude/agents/<name>.md` | `claude-user/agents/<name>.md` | tool names translated; unknown tools dropped (an unknown tool name can hang a subagent) |
| Enabled plugins (`enabledPlugins`) | `claude-<plugin>/` | found in `plugins/cache/` or in place in the marketplace clone |
| MCP `command` servers | `mcp_config.json` | unchanged |
| MCP `url` / `type: http` servers | `mcp_config.json` with `serverUrl` | |
| `CLAUDE.md` | `AGENTS.md` inline include | |
| Hooks | `hooks.proposed.json`, disabled | event names and the stdin/stdout contract differ, so scripts need rewriting |
| `permissions.allow` | `MIGRATION_NOTES.md` | suggestions only, never applied |
| Chat history, credentials, model settings | not moved | |

## Why not just `agy plugin import claude`?

agy has a built-in Claude importer. On a current Claude Code install it finds nothing. Tested on Claude Code 2.1.295 with agy 1.3.2:

```
$ agy plugin import claude
No claude extensions found.
```

Current Claude Code keeps enabled plugins in `plugins/cache/<marketplace>/<plugin>/<version>/` or loads them in place from the marketplace clone. The importer only looks one level under `~/.claude/plugins/`. In a hand-built flat layout, it also replaced a remote MCP server's URL with an empty `command`. This plugin handles both cases and also covers user skills, CLAUDE.md, and settings-level hooks and permissions.

## Run the scripts directly

The skill uses two standard-library Python scripts (Python 3.9+):

```bash
S=~/.gemini/config/plugins/claude-migrate-to-agy/skills/migrate-from-claude/scripts
python3 $S/discover.py --project ~/code/my-service > /tmp/inv.json
python3 $S/apply.py --inventory /tmp/inv.json            # dry run
python3 $S/apply.py --inventory /tmp/inv.json --apply --plan-id <id-from-dry-run>
```

## Tests

```bash
python -m pytest -q          # or, without pytest:
python3 tests/run_stdlib.py
```

The tests build real Claude Code directory layouts in temp folders, run the scripts, and check the files they write. A static check fails the build if the scripts import mocking libraries.

## Sources

- Antigravity docs: [Plugins](https://antigravity.google/docs/plugins), [Skills](https://antigravity.google/docs/skills), [Rules](https://antigravity.google/docs/rules), [Hooks](https://antigravity.google/docs/hooks), [Subagents](https://antigravity.google/docs/subagents), [MCP migration](https://antigravity.google/docs/cli/gcli-migration), [Changelog](https://antigravity.google/docs/changelog)
- Prior art: [yuting0624/antigravity-for-claude-code](https://github.com/yuting0624/antigravity-for-claude-code) documented Claude Code's storage layout and the importer's gaps. No code from it is used here.

## License

Apache-2.0
