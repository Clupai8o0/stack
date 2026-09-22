# Global Codex context

This file is generated. Its source is this repo: `agents/codex/DELTA.md`
(this part) plus `rules/CLAUDE.md` (everything after the line).
Edit those, then run `python3 scripts/sync-agents.py`. Never edit
`~/.codex/AGENTS.md`, `~/AGENTS.md` or a project's `AGENTS.md` — they are symlinks.

## You are a fourth agent on a shared Mac

Three Claude Code accounts (`claude`, `cx`, `cm`), OpenCode, dsh and you run on the same repos, often at the same
time. You share their claims, queue and guardrail files, so the coordination rules below are yours
too. `claims.py list` labels your sessions `codex`.

## What is the same

| | |
|---|---|
| Rules | Everything after the line, including the hard rules, code style and document style |
| Project briefings | A repo's `AGENTS.md` is a symlink to its `CLAUDE.md`. Same file, same authority |
| Claims, queues, guardrails | Same `hooks/` scripts, same JSON files. A rule one agent learns applies to you |
| Skills | `~/.codex/skills/` has the same core skills; the vault library loads on demand the same way |
| Commands | `~/.codex/prompts/` has the same slash commands |
| Memory | Relevant memories from every Claude account are recalled into your context per prompt (`hooks/memory_recall.py`). Durable learnings go in the project's `CLAUDE.md` or the vault, never only in session memory |

## What is different

- **Effort.** You have no `/effort`. Yours is `model_reasoning_effort`, set with `/model` in the TUI
  or `codex -c model_reasoning_effort=<low|medium|high>`. When the effort gate asks for more, say so
  and stop rather than pretending to switch.
- **Session registry.** Codex does not write one, so `hooks/codex_hook.py` writes
  `~/.codex/sessions/<pid>.json` for you. That is what makes other sessions see your claims.
- **Hook trust.** Codex refuses to run `hooks.json` until you approve it once in an interactive
  `codex`. Until then `codex exec` hangs. Approve it once after any change to `codex/hooks.json`.
- **You are also the second opinion.** Claude calls you through
  `scripts/second_opinion.py` and `mcp__codex__codex` as an independent
  reviewer. When you are the reviewer, review — do not re-plan the work.
- **Model policy still applies to you.** You are the only outside model allowed on confidential
  folders (`projects/client-work/**`, `notes vault`, anything unlisted). Do not hand those on to
  DeepSeek, OpenCode or Gemini.
- **Auto-memory.** Yours is separate from all three Claude accounts and invisible to them. Anything
  another session needs must be written to the project or the vault.

## Before you start work in a repo

```
python3 $CLUPAI_HOME/hooks/claims.py list
python3 $CLUPAI_HOME/hooks/claims.py history --grep WORD
python3 $CLUPAI_HOME/hooks/guardrails.py list --dir <project>
```

Then keep a status note as you go: `claims.py note "doing now; done/tried: ..."`, run inside the repo.
