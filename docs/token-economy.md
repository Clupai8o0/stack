# Token economy

**Plan with Opus, pin low effort on workers, prove it with tests or a second model, write what you learned into the project, and keep few sessions open.**

The enforced version is the `token-economy` skill (`skills/_core/token-economy/SKILL.md`). This page is the why.

## The measured rules

| Rule | Evidence (Sep 2026) |
|---|---|
| Default subagent: Opus 5, low effort | 96-100% on every scored task, $0.33/task |
| Always pass `model` and `effort` to subagents | They inherit session effort; xhigh costs 2.7x ($0.90 vs $0.33) with no meaningful gain |
| Opus 5 high only for text people read, and final judging | Best writing, 8.8/10, $0.53/task |
| Sonnet 5 only when a test decides pass/fail | Missed 25% of review defects and invented facts in prose |
| No "cheap model works, Opus reviews" | $0.78 vs $0.33 for Opus low alone, same score |
| Do one-line lookups inline, not in an agent | Every agent starts with setup tokens (estimate: 30-50k) |
| Reset context near 200k | A call at 600-900k costs about 3x a call at 200k (estimate: $0.38 vs $0.13) |
| `/compact` and `/clear` + handoff cost the same | $0.81 vs $0.69 at 563k; pick the handoff if another session continues |
| Don't switch effort mid-session on Opus 5 | It drops the prompt cache: the next call re-reads everything at full price |
| Review with an outside model | Codex review costs almost no Claude usage (GPT-6 Astra since the bake-off) |
| Keep agents short; split past ~100 steps | 10 long agents were 71% of past Opus usage |
| Keep installed skills small | The skill listing cost tokens in every agent (estimate: ~7.5k before cleanup) |
| Recall memories per prompt, don't preload | `memory_recall.py` keeps `MEMORY.md` to pinned notes only |

## Start a session

1. Open it in the project folder, so its `CLAUDE.md`, skills and model policy load.
2. One session per task.
3. Write the first prompt with this template:

```text
Goal: <one sentence>
Done when: <tests pass / page shows X / numbers match Y>
Scope: <dirs or files>. Don't touch: <dirs, branches, deploys>
Context: read <AGENTS.md / handoff.md> first, nothing else up front
Run it: workers claude-opus-5 low, final check opus-5 high or Codex, max 4 agents at once
Finish with the recap. No questions mid-work; put decisions in the recap checklist.
```

4. Anything touching several files starts in plan mode.
5. Finish cleanly: handoff to `<project>/.claude/handoff.md`, lessons to the project `CLAUDE.md`, then exit.

## Choose the mode

| Situation | Main session | Workers | Checked by |
|---|---|---|---|
| Quick question or small edit | normal effort, no agents | none | you |
| Normal feature or bug | high effort | Opus 5 low, pinned | tests, then Codex |
| Big audit, migration, wide sweep | workflow | Opus 5 low, pinned per stage | Opus 5 high or Codex |
| Text someone else reads | Opus 5 high writes it | none | Codex or a second Opus pass |

## Unclear scope

1. Scope at `/effort xhigh` in its own short session. Write the result to a file.
2. `/clear`, `/effort high`, and execute from that file with Opus 5 low workers.

## Mixed-model flow (open and personal projects only)

1. Claude (Opus 5 high) writes the spec and acceptance tests into the repo.
2. Make a disposable worktree: `git worktree add ../wt-<task> -b <task>`.
3. Delegate: `second_opinion.py spec.md --cwd ../wt-<task> --write --ladder dsh:deepseek-flash`.
4. Claude runs the tests, reads the diff, fixes small things, merges. Then a review pass.
5. Only worth it when the spec is clear and the change is big. For small edits, start-up and review cost as much as doing it.

## Other hygiene

- Point at files and logs instead of pasting them.
- Browser work: use page text or the accessibility tree; screenshots only as evidence.
- Close idle sessions: `memory_guard.py close-idle 6`.
