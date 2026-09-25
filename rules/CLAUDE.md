# Global agent rules (template)

This file is the global `CLAUDE.md` for every Claude Code account on this machine, and the base of the
`AGENTS.md` that `scripts/sync-agents.py` generates for Codex, OpenCode, dsh and kimi.
Its home is `$CLUPAI_HOME/rules/CLAUDE.md`. `~/.claude/CLAUDE.md` is a symlink to it. Edit it here.

Lines marked **Example:** are one person's setup. Replace them with yours or delete them.

## Many sessions, one brain

Several agent sessions often run on the same projects at the same time. They share auto-memory (`hooks/memory_recall.py` links a second account's memory folders to
the main account's at session start, merging their files), but NOT per-account skills or settings, and outside
models have no auto-memory. Anything they all need must be written somewhere they can all read.

**Example:** two Claude Code accounts on one Mac: `claude` (main, `~/.claude`) and `cx` (second login,
`~/.claude-exec`, the only second-account folder the scripts support), plus Codex, OpenCode, dsh and kimi.

- **Durable learnings never live only in auto-memory.** Write them where the next session will read them:
  - about one project: that project's `CLAUDE.md` (or a doc it links to)
  - about how you work, tools, or more than one project: your shared notes folder, linked from an index
- **Handoffs between sessions go in the project**, e.g. `<project>/.claude/handoff.md`, not in chat and not
  in account memory. Read it at the start of work on that project.
- **Skills:**
  - core skills, installed in every account: `$CLUPAI_HOME/skills/_core/`, symlinked in
  - everything else: your library `$CLUPAI_HOME/skills/<category>/`, loaded on demand with the `vault-skills` skill
    (this repo ships only `_core`; the library is yours to add)
  - skills one project uses often: symlink into `<project>/.claude/skills/`
  - never install a skill into one account's skills folder directly
- **Scope tools to where they are used.** Skills, MCP servers, connectors and API keys go in the smallest place
  that works (project, core, or library). Follow the `tool-scoping` skill.
- **Memory:** a hook blocks new agents, workflows and heavy commands when the machine is out of memory.
  `python3 $CLUPAI_HOME/hooks/memory_guard.py list` shows every open session; `close-idle 6` closes idle ones.

## Parallel sessions on one repo

- A hook claims each file a session edits, in `<git common dir>/claude-claims.json` (shared by all worktrees and
  all accounts). It blocks editing a file another live session is editing in the same worktree, and warns when it
  is being edited in another worktree.
- Before starting, run `python3 $CLUPAI_HOME/hooks/claims.py list` and avoid claimed files.
- Keep a status note with `claims.py note "doing now; done/tried: ..."` (run inside the repo). A hook reminds you
  after 3 turns with edits and no note.
- Before starting something, check `claims.py history --grep WORD` for whether it was already done or tried.
- Claims drop when the session ends, or with `claims.py release`.
- On overlap, talk to the other session directly: the name in `claims.py list` is its `SendMessage` address.
  Send one handoff (done, branch, files, tests, what is left). The session with less done hands over, releases,
  and stops or moves on. If you can't agree in two messages, or the other session is not reachable, tell the user.
- Bash `sed -i` / `perl -i` on plain paths is tracked. Other Bash writes (scripts, redirects, heredocs) are not,
  so use Edit/Write for repo files when others are running.

## Shared deploys, merges and learned guardrails

- `queues.py` gives one session at a time a turn on a shared target (`myapp:deploy-staging`):
  `join NAME "what"`, then `wait NAME` as a background task so you are woken on your turn, then
  `done NAME "result"`. Guardrails queue matching commands on their own; a blocked session is already in line.
- `guardrails.py` rules (`$CLUPAI_HOME/guardrails.json`) warn, STOP, queue or deny commands and file edits.
  Before executing a plan in a project, run `guardrails.py list --dir <project>` and say which could apply.
- On STOP: check the edge case. If it applies, fix it or tell the user in one line what could break and what you
  recommend. If it doesn't, `guardrails.py ack ID "what you checked"` and retry. Never work around it.
- After a failure another session could repeat, add a rule:
  `guardrails.py learn --action stop --match '^...' --dir <project> --why "could break: ...; instead: ..."`.
  Retire rules that turn out wrong.

## Say whether the session can be closed

When a piece of work ends, run `claims.py closeout` (lists the claims, notes and queue places this session still
holds) and end the reply with one close line:

- `Safe to close: nothing left to do.` Only when there are no follow-ups and closeout says clear.
- `Safe to close, handover ready: <path>` when there are follow-ups. First write them to
  `<project>/.claude/handoff.md` (what is done, what is left in order, branch, commands, open risks, which
  guardrails apply), finish or leave every queue place, then `claims.py note "handed over: <path>"` and
  `claims.py release`, and run closeout again.
- `Not safe to close: <reason>` when background work is running, a queue turn is held mid-action, or you are
  waiting on the user. Say what would make it safe.

## One layer for every agent

- Shared files (this file, output styles, core skills) live in `$CLUPAI_HOME` and are symlinked into every
  config dir. Change the shared copy, never a per-account copy.
- Codex, OpenCode, dsh and kimi each get a generated `AGENTS.md` (this file plus `agents/<agent>/DELTA.md`), the
  same skills and commands by symlink (Grok Build reads the Claude files itself and gets only its delta), and hooks through `hooks/agent_hook.py --agent <name>`, which calls the same
  claims, queues, guardrails, memory-guard and memory-recall scripts. Their sessions show in `claims.py list`.
- With `--repos`, every repo with a `CLAUDE.md` gets a git-ignored `AGENTS.md` symlink to it, at the repo's git
  top level. A folder holding an empty `.sync-agents-skip` file is skipped.
- After changing this file, a core skill, a command or any agent's hook config, run
  `python3 $CLUPAI_HOME/scripts/sync-agents.py` (`--check` first).
- **Codex will not run a changed `hooks.json` until you launch `codex` once interactively and approve it;
  until then `codex exec` hangs.**
- **Memory is recalled, not preloaded.** `MEMORY.md` holds only pinned memories (type `feedback` or `user`, or a
  line marked `(pinned)`). `hooks/memory_recall.py` folds the rest out at session start and, on each prompt, adds
  the few memories that match it, from every account's memory for this folder and the folders above it.
  Search with `memory_recall.py search WORDS`. Link related memories with `[[name]]`: a linked memory is recalled
  alongside its match. Outside models only get memories from folders `model-policy.json` lets them see.

## Token economy

Before spawning any subagent or workflow, follow the `token-economy` skill. Short form, measured Sep 2026:

- Default worker: **Opus 5.5 at low effort.** Always pass `model` AND `effort`, or subagents inherit the session
  effort (xhigh under ultracode: 2.7x cost, no meaningful gain).
- Sensitive writing and final judging: Opus 5.5 at high effort.
- Sonnet 5 only where a test or script decides pass/fail. It invents facts in prose and misses review defects.
- Codex for independent verification: same quality, almost no Claude usage.
- "Cheap model executes, Opus reviews" costs more than Opus low alone. Don't.
- **Reset context near 200k tokens**, with `/clear` plus a handoff or with `/compact`. They cost about the same,
  but every call at 600-900k costs about 3x a call at 200k (estimate). Prefer the handoff when another session may continue.
- **Effort gate:** a hook makes every substantive reply start with `Effort: <now> now, <needed> needed (...)`.
  Early in a session, Claude stops and asks for `/effort <needed>` when the gap matters; later it only notes it.
- **Effort is set by the user, not by Claude.** Changing it mid-session on Opus drops the prompt cache, so
  suggest switching only right after a `/clear`. Agents get their own `effort`.

## Outside models

Policy by folder lives in `$CLUPAI_HOME/model-policy.json`. Unlisted folders are confidential.

| Policy | Example folders | Allowed outside Claude |
|---|---|---|
| confidential | client work, private notes, anything unlisted | Codex only |
| open | open-source and study repos | Codex, DeepSeek (`dsh`), Kimi (`kimi`), OpenCode on OpenRouter |
| personal | your own side projects | the above, plus Gemini (`agy`) and Grok Build (`grok`, plan quota) |

Who does what (from the Sep 2026 bake-off, full table in the `token-economy` skill): **Opus 5.5** is the main worker
and the final reviewer (high effort); **Fable 5.1** takes only the hardest coding tasks. **DeepSeek V4 Flash** is the bulk worker, always
followed by a review pass. Reviewers are **DeepSeek Flash + Opus 5.5 + GPT-6 Astra (Codex)**; Grok Build stands in
when Codex is out of quota. Kimi K3 is a backup.

Always call outside models through the script, so policy, sandbox and fallback apply:

```
python3 $CLUPAI_HOME/scripts/second_opinion.py PROMPT_FILE --cwd <project dir> [--network] [--write] [--ladder ...]
```

- Routes: `codex`, `dsh:<model>`, `kimi:<model>`, `opencode:openrouter/<vendor>/<model>`, `agy:<model>`, `grok:<model>`.
- Don't call the raw CLIs on client or sensitive folders. `kimi -p` runs every tool without asking, so only ever
  call it through the script.
- Prefer CLIs over MCP servers for outside models: a CLI costs nothing until it is called.

## Code review

- **Always review code with a second model, tiered to save Codex quota.** Before calling a change done, run
  `second_opinion.py - --cwd <repo> --review` over the diff, on top of your own tests, and fold in what it confirms.
  - Every change: `--ladder dsh:deepseek-flash` where policy allows it.
  - Risky changes (auth, security, database rules, payments, migrations or deletes, concurrency, shipped to a
    client, or over ~300 lines) also get `--ladder codex`.
  - Codex out of quota: an Opus 5.5 review subagent at high effort.

## Documents for the user's review

Anything the user has to read (reports, plans, summaries) must be **short**. Main idea only.

- Lead with the answer in one line. Conclusion first, never reasoning first.
- Assume the reader does not know the subject. Plain language; gloss the jargon or drop it.
- One sentence per point. If a point needs a paragraph, it needs a better sentence.
- Tables and lists over prose. Cut citations down to the one that proves the point.
- Detail is available on request. Offer it; do not pre-emptively include it.

Depth in the underlying work is still expected. Just do not make the user read it.

## Environment (example, replace with yours)

- **Example:** terminal is kitty; `ui/palette.py` reads its accent colour so the statusline matches.
- **Example:** `ls` is aliased to eza, so scripts use `command ls`.
- **Example:** macOS gates some writes (LaunchServices, `/Applications`) on the terminal app hosting the shell.

## Code style (example)

- **Example:** K&R braces in every language that has them. Function definitions take the brace on its own line.
  Where a file already runs a different style, match the file. Never reformat as a side effect.
