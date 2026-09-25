# Agents

**Six agent CLIs share one layer: the same rules, core skills and coordination hooks. Claude Code does the work, the others review, take bulk jobs, or stand in when a quota runs out.**

`scripts/sync-agents.py` wires every agent except Claude Code (which `install.sh` links). Run it with `--check` first.
Outside agents are called through `scripts/second_opinion.py`, which applies `model-policy.json`, the sandbox and the fallback ladder.

| Agent | Rules | Skills | Hooks | Used for | Quirks |
|---|---|---|---|---|---|
| **Claude Code** (`claude`, a second account `cx`) | `~/.claude/CLAUDE.md` symlinks to `rules/CLAUDE.md` | `~/.claude/skills/` links to `skills/_core/`; the library loads on demand | Native, in `settings.json` (`examples/claude-settings.example.json`) | Main worker (Opus 5.5 low), final reviewer (Opus 5.5 high), hardest coding (Fable 5.1) | Accounts share memory, not skills or settings. Cross-account messaging needs `session_mirror.py` running. Self-reset (`reset.py`) works only in kitty. Every account auto-compacts at 250k |
| **Codex** (`codex`) | Generated `~/.codex/AGENTS.md` = `agents/codex/DELTA.md` + rules; also `~/AGENTS.md` | `~/.codex/skills/`, commands in `~/.codex/prompts/` | `~/.codex/hooks.json` -> `codex_hook.py` -> `agent_hook.py` | Independent reviewer; risky changes (auth, rules, payments, migrations, >300 lines); the only outside model allowed on confidential folders | **Won't run a changed `hooks.json` until you launch `codex` once interactively and approve it; until then `codex exec` hangs.** No `/effort`: use `model_reasoning_effort`. Writes no session registry, so `agent_hook.py` writes one for it |
| **OpenCode** (`opencode`) | Generated `AGENTS.md` = `agents/opencode/DELTA.md` + rules | `~/.config/opencode/skills/`, `commands/` | Plugin `shared-agent-layer.js` | Second opinions from GLM, Qwen, Grok or MiniMax on OpenRouter; open and personal folders | Never used for DeepSeek or Kimi (they have their own CLIs). A tool call the plugin blocks is a deny |
| **dsh** (DeepSeek's CLI) | Generated `AGENTS.md` = `agents/dsh/DELTA.md` + rules | `~/.dsh/skills/` | `agents/dsh/hooks.json`, inserted into each dsh profile | Default bulk worker (DeepSeek V4 Flash), always followed by a review; standing reviewer; cheap image input | **Hooks only fire when it may write (`second_opinion.py --write`).** In the read-only sandbox they are skipped, which is harmless since nothing is edited |
| **kimi** (Kimi Code CLI) | Generated `AGENTS.md` = `agents/kimi/DELTA.md` + rules | `~/.kimi-code/skills/` | Managed `[[hooks]]` block in `~/.kimi-code/config.toml` | Backup when DeepSeek is down (Kimi K3) | **`kimi -p` runs every tool without asking, so only call it through `second_opinion.py`.** Paid per call. It drops SessionStart output, so `agent_hook.py` resends that context later |
| **Grok Build** (`grok`) | Reads `~/.claude/CLAUDE.md` itself (Claude compatibility), plus `agents/grok/DELTA.md` linked as `~/.grok/rules/shared-agent-layer.md` | Reads `~/.claude/skills/` itself | Managed block in `~/.grok/config.toml` calling `agent_hook.py --agent grok` | Second reviewer on personal folders; stands in for Codex when it is out of quota; plan quota | **Claude-compat hooks are off** (`[compat.claude] hooks = false`), or grok would run the Claude hooks as if it were Claude. It drops session and prompt hook output, so that context arrives with its first tool call. Personal folders only |

## Common to all

- **Claims, queues and guardrails are shared.** A session of any agent can block, or be blocked by, any other on the same file. `claims.py list` labels each one.
- **Memory is recalled, not written.** Outside agents get matching memories per prompt but keep none; durable findings go in the project's `CLAUDE.md`.
- **When an outside agent reviews, it reviews.** Findings only, no re-planning.
- **Policy by folder.** Confidential: Claude and Codex only. Open and personal: DeepSeek, Kimi, OpenCode too. Grok and Gemini (`agy`): personal only.
- **After changing rules, a core skill, a command or any hook config**, run `python3 scripts/sync-agents.py` (then approve Codex's hooks once).

Gemini (`agy`) is a route in `second_opinion.py` for free drafts and reads, not a synced agent: it gets no rules or hooks.
