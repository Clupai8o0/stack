# Architecture

**One folder holds the rules, hooks and skills. Every agent links to it. Hooks coordinate sessions through small JSON files.**

## Layout at runtime

```mermaid
flowchart LR
  subgraph home["$CLUPAI_HOME (this repo)"]
    R[rules/CLAUDE.md]
    D[agents/*/DELTA.md]
    H[hooks/*.py]
    S[skills/_core/*]
    P[model-policy.json<br/>guardrails.json]
  end
  SY[scripts/sync-agents.py]
  R --> SY
  D --> SY
  S --> SY
  SY -->|AGENTS.md, skills, hooks| CX[Codex]
  SY --> OC[OpenCode plugin]
  SY --> DS[dsh]
  SY --> KM[kimi]
  SY -->|DELTA.md, config hooks| GK[grok]
  R -->|symlink via install.sh| CC[Claude Code accounts]
  H -.hooks.-> CC
  H -.agent_hook.py.-> CX & OC & DS & KM & GK
  SO[scripts/second_opinion.py] -->|policy check, sandbox, fallback| CX & DS & KM & OC & GK
  P --> SO
```

## Shared state

| File | Written by | Holds |
|---|---|---|
| `<git common dir>/claude-claims.json` | `claims.py` | who is editing which file, status notes, history |
| `~/.local/state/claude-claims/` | `claims.py`, `queues.py` | queue lines, per-session index, guardrail acks |
| `$CLUPAI_HOME/guardrails.json` | `guardrails.py learn` | warn/stop/queue/deny rules learned from failures |
| `$CLUPAI_HOME/model-policy.json` | you | which outside models may see which folders |
| `~/.claude*/projects/*/memory/` | Claude Code | memories; `memory_recall.py` reads all accounts |

## Hooks

| Hook | Event | What it does |
|---|---|---|
| `claims.py` | PreToolUse, SessionStart, UserPromptSubmit, SessionEnd | Claims files per session; blocks same-worktree edits; runs guardrails and queues; nags for a status note |
| `queues.py` | called by guardrails, or by hand | One session at a time on a shared target such as a staging deploy |
| `guardrails.py` | called by `claims.py pretool` | Checks each command and edit against learned rules |
| `memory_guard.py` | PreToolUse, SessionStart | Blocks new agents and heavy commands when RAM or swap is low |
| `memory_recall.py` | SessionStart, UserPromptSubmit | Adds only the memories that match this prompt |
| `effort_gate.py` | UserPromptSubmit | Makes the model state the effort a task needs; early on, asks you to change it |
| `agent_hook.py` | every event, non-Claude agents | Adapter so Codex, OpenCode, dsh, kimi and Grok Build run the same hooks |
| `codex_hook.py` | Codex | Thin shim to `agent_hook.py` |

## Design choices

- **Symlinks, not copies.** One edit reaches every agent. `sync-agents.py --check` shows drift.
- **Fail open.** A broken hook never blocks work, except a deliberate deny.
- **Files, not a server.** State is JSON with file locks. Nothing to run or keep alive.
- **Policy by folder.** The script decides which outside models are allowed from `--cwd`, not the caller.
- **Grok Build reads Claude's files itself.** It loads `~/.claude/CLAUDE.md` and skills through its Claude compatibility, so
  it gets only `agents/grok/DELTA.md` (linked as `~/.grok/rules/`) and a hooks block in `~/.grok/config.toml`.
- **Opt out a folder with `.sync-agents-skip`.** `sync-agents.py --repos` links `AGENTS.md` to each repo's `CLAUDE.md`;
  a folder with this empty file is skipped. `rules/` carries one because its `CLAUDE.md` is a template, not a briefing.
- **Public-copy safety.** UI changes need `--ui`, repo links need `--repos`, and backups are timestamped.
- **macOS first.** The sandbox for outside CLIs uses `sandbox-exec`, and `memory_guard.py` reads macOS memory stats.
