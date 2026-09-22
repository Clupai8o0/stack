# Model routing benchmark (17 Sep 2026)

**Opus 5 at low effort is the best default subagent. Push second opinions and reviews to Codex or a cheap outside model.**

## Method

- **Tasks:** five, built from real study and open-source work, with planted bugs and defects so they could be scored: fact-check (18 claims), bug fix (4 planted bugs, 29 hidden
  tests), claim audit (13 subtle claims), PR review (4 planted defects), and a mentor update judged blind.
- **Setups:** 23. Claude models at several effort levels, a two-stage "cheap model works, Opus reviews" setup, and
  11 outside models.
- **Runs:** 2 per setup. Tokens read from transcripts, cost at API list price.
- **Judges:** Opus 5 and Codex, blind. They ranked the writing almost identically (correlation 0.9).

## Claude

| Setup | Fact-check | Bug fix | Audit | Review | Writing /10 | $/task |
|---|---|---|---|---|---|---|
| **Opus 5 low** | 100% | 100% | 96% | 100% | 8.0 | **0.33** |
| Opus 5 medium | 100% | 100% | 100% | 100% | 7.8 | 0.42 |
| **Opus 5 high** | 100% | 100% | 100% | 100% | **8.8** | 0.53 |
| Opus 5 xhigh | 100% | 100% | 100% | 100% | 6.8 | 0.90 |
| Sonnet 5 low | 100% | 100% | 96% | 75% | 4.0 | 0.24 |
| Sonnet 5 high | 100% | 100% | 100% | 88% | 3.8 | 0.36 |
| Fable 5.1 high | 100% | 100% | 100% | 100% | 7.8 | 1.13 |
| Opus 4.8 high | 100% | 100% | 100% | 75% | 3.8 | 0.77 |
| Haiku 4.5 high | 78% | 100% | 100% | 38% | 2.5 | 0.18 |

- Sonnet 5 invents links between facts at every effort level.
- Sonnet does the work, Opus low reviews: 100%, but $0.78. Opus low alone: same score, $0.33.
- Every agent pays setup tokens before it does anything (estimate: 30-50k). Fewer installed skills and MCP servers cut that for every agent.

## Outside models

| Route | Review | Audit | Writing /10 | $/task* |
|---|---|---|---|---|
| Codex (gpt-5.6-sol at the time) | 100% | 100% | judge | plan quota |
| DeepSeek V4 Pro | 100% | 100% | 5.0 | 0.06 |
| GLM 5.3 | 100% | 100% | 7.5 | 0.27 |
| Kimi K3 | 100% | 100% | 6.0 | 0.28 |
| Gemini 3.8 Flash | 100% | 100% | 5.5 | plan quota |
| Grok 4.6 | 88% | 100% | 7.2 | 0.26 |
| Qwen 3.8 Max | 88% | 96% | 7.2 | 0.26 |
| DeepSeek V4 Flash | 88% | 100% | 4.0 | 0.02 |
| MiniMax M3 | 62% | 100% | 4.0 | 0.05 |

\*List-price equivalent.

- Outside models are 3-10x slower than a Claude agent. Run them in parallel worktrees, not in line.
- Several routes may train on or reuse what you send. That is why `model-policy.json` exists.

## Coding

Two tasks: a feature with 31 hidden tests, and 4 planted bugs with 29 hidden tests. 2 runs each.

| Coder | Feature | Bugs | $/task* | Time |
|---|---|---|---|---|
| Claude Opus 5 low | 100% | 100% | 0.39-0.45 | 30-35s |
| Claude Sonnet 5 low | 100% | 100% | 0.18-0.23 | 50s |
| Codex (gpt-5.6-sol) | 100% | 100% | plan quota | 115-130s |
| DeepSeek V4 Flash | 100% | 100% | **0.01-0.02** | 95-240s |
| DeepSeek V4 Pro | 100% | 100% | 0.05-0.07 | 100-200s |
| GLM 5.3 | 100% | 100% | 0.16-0.29 | 100-310s |
| Kimi K3 | 100% | 100% | 0.29-0.54 | 85-300s |
| Gemini 3.8 Flash | 100% | 100% | 0.55-0.71 (quota) | 240-290s |

- Every coder passed these clearly specified single-file tasks, so choose on price, speed and privacy.
- Large multi-file changes were tested later, in the [bake-off](bake-off.md).

## Code-graph context tool (tried, no saving)

A repo-indexing context tool cost $0.47 per question against $0.43 without it, at the same correctness (8.0/10).

## Where Claude and Codex are blocked

- DeepSeek and Kimi on their direct APIs still work; OpenCode on DeepSeek V4 Flash is a workable setup.
- Confidential folders have no allowed model there, so plan that work for later.

## Caveats

- Two runs per setup (this benchmark only). Tasks were 3-60 steps, not 300-step jobs.
- Codex later moved to GPT-6 Astra, which the [bake-off](bake-off.md) used.
- API list price stands in for plan usage.
- Re-run when models change.
