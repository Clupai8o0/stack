---
name: tool-scoping
description: Put skills, MCP servers, claude.ai connectors and API credentials only where they are used, so every session and subagent starts small. Use when installing or removing a skill, MCP server, plugin or connector, when a project needs a tool other projects don't, when session start-up feels heavy, or when auditing what loads in either Claude account.
---

# Tool scoping

Everything installed at user level loads into every session and every subagent in that account.
The listing alone was about 7.5k tokens per agent before the September 2026 clean-up. Scope tools to
the smallest place that works, and do it the same way in both accounts.

## Where each thing goes

| Thing | Used by one project | Used by most sessions | Rarely used |
|---|---|---|---|
| Skill | symlink `<project>/.claude/skills/<name>` into the vault library | `skills/_core/<name>`, symlinked into `~/.claude/skills` and `~/.claude-exec/skills` | vault library only, loaded with `vault-skills` |
| MCP server (local) | `<project>/.mcp.json`, or `claude mcp add -s project` | `claude mcp add -s user`, in both accounts | don't add one; call its CLI from Bash |
| claude.ai connector | keep it on, then disable it in every other project (see below) | leave on | `deniedMcpServers` in the account's `settings.json` |
| API key or credential | project `.env` (git-ignored) or the project's secret store | never in global settings | not stored |
| Plugin | enable in `<project>/.claude/settings.json` | `enabledPlugins` in both accounts | disabled |

## Commands

- **Skill into a project:**
  `ln -s $CLUPAI_HOME/skills/<category>/<name> <project>/.claude/skills/<name>`
  If `.claude/` is tracked by git, also add `.claude/skills/<name>` to `<repo>/.git/info/exclude`.
- **Core skill into both accounts:** symlink into both skills folders, then run
  `python3 $CLUPAI_HOME/scripts/build_index.py`.
- **Hide a skill without deleting it:** `"skillOverrides": {"<name>": "off"}` in that account's `settings.json`.
- **Block a connector everywhere:** `"deniedMcpServers": [{"serverName": "claude.ai <Name>"}]`.
  Nothing overrides a block, so don't use it for a connector one project still needs.
- **Connector for some projects only:** add `"claude.ai <Name>"` to `projects["<path>"].disabledMcpServers`
  in that account's `.claude.json` for every other project. This is what `/mcp` writes.
  Brand-new projects still load the connector until toggled.
- **Remove a user MCP server:** back up its JSON from `.claude.json` first, then
  `claude mcp remove <name> -s user` (for the cx account, prefix `CLAUDE_CONFIG_DIR=~/.claude-exec`).
- **Outside CLIs instead of MCP servers:** `opencode`, `agy` and `codex` are called from Bash through
  `token-economy/scripts/second_opinion.py`. That costs no tokens until used.

## Audit

1. List what loads: both skills folders, `enabledPlugins`, `mcpServers` and `claudeAiMcpEverConnected`
   in both `.claude.json` files, and each project's `.mcp.json`.
2. Count real use in the last 60 days: `Skill` and `mcp__<server>__` tool calls in each account's
   `projects/**/*.jsonl` transcripts.
3. Move anything unused to the vault or disable it, keeping a backup under `~/.claude-config-backups/<date>/`.
4. Write what changed into `skills/CHANGES.md`, so the other account knows.

## Rules

- Change both accounts together, or say which one you changed and why.
- Never delete a skill or server config without a backup. Moving to the vault counts as a backup.
- UI and design skills are never core. Link them per project.
