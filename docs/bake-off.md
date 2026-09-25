# Model bake-off (Sep 2026)

**Opus 5.5 is the main worker and the Claude reviewer (since 23 Sep). Fable 5.1 takes only the hardest coding tasks. GPT-6 Astra (Codex) and DeepSeek V4 Flash are the other keepers. Kimi K3 is backup only. DeepSeek V4 Pro and OpenCode Go were dropped.**

All work came from the git history of a production React Native + Firebase app. The runner is in `bakeoff/`.

## Who does what now (23 Sep 2026)

| Model | Route | Role | Why |
|---|---|---|---|
| Opus 5.5 | Claude Code | **Main worker** (low effort), **final reviewer** (high effort) | 10/10 tasks for $16.12 (22% less than Opus 5); best Claude reviewer |
| Fable 5.1 | Claude Code | Hardest coding tasks only | Cleanest code, but a weaker and ~2.7x costlier reviewer than Opus 5.5 |
| Opus 5 | Claude Code | Fallback if 5.5 is unavailable | 10/10, $20.60 |
| GPT-6 Astra | `codex` | Independent reviewer; risky changes | Fastest coder; found 1 bug no one else did |
| DeepSeek V4 Flash | `dsh:deepseek-flash` | Bulk worker and reviewer; always followed by a review | 10/10 for ~$1.50; most review bugs (9 of 17) |
| Grok 4.7 | `grok:grok-4.7` | Second reviewer on personal folders; stands in when Codex is out | 10/10 coding on the plan quota |
| Gemini 3.8 Flash | `agy:<model>` | Free quick drafts and reads | 9/10, plan quota |
| Kimi K3 | `kimi:kimi-k3` | Backup if DeepSeek is down | 10/10 but slowest, ~$14.70 |

Review trio: **DeepSeek Flash + Opus 5.5 + Astra** (Grok 4.7 as well on personal folders).

## What was tested

| Test | What |
|---|---|
| Coding | 10 real tasks from git history: 6 short, 4 long (one touched 20 files). Each was checked to fail before the real commit and pass after it. |
| Review | 5 real changes whose tests passed, containing 17 bugs a later review caught. Graded blind. |
| Code quality | Astra reviewed each model's passing code on the 2 long backend tasks, blind. |

## Opus 5.5 vs Fable 5.1 as reviewer (23 Sep)

**Opus 5.5 at high effort replaced Fable 5.1 as reviewer.** Same 5 review cases, both at high effort, 2 runs each.
All 20 answers were re-graded blind by an Opus 5.5 subagent and by Grok 4.7 (Codex was out of quota). Both graders agree.

| | Opus 5.5 | Fable 5.1 |
|---|---|---|
| Bugs found, run 1 / run 2 (Opus grader) | **10 / 9** | 6 / 4 |
| Same, Grok grader | **9 / 8** | 6 / 4 |
| False alarms | 0 | 0 |
| Cost per 5 reviews | **~$4.35** | ~$11.80 |
| Time per 5 reviews | **~12 min** | ~19 min |
| Bugs it found that the other missed (both runs) | 4-5 | **0** |

- Opus 5.5's code quality has not been judged yet (that needs a Codex run).
- Opus 5.5 took about 30% more wall time than Opus 5 on the coding tasks.

## Full results

Opus 5.5 row added 23 Sep. All other rows are the 22 Sep run.

| Model | Coding | Review (of 17 bugs) | Code quality (defects, lower is better) | Cost for 10 tasks | Role |
|---|---|---|---|---|---|
| **Opus 5.5** | 10/10 | **8** (first run) | not yet judged | **$16.12** | **Main worker, final reviewer** |
| Opus 5 | 10/10 | not run | 32 | $20.60 | Fallback (was main worker until 23 Sep) |
| Fable 5.1 | 10/10 | 6 | **28** | $26.95 | Hardest coding only (was final reviewer until 23 Sep) |
| GPT-6 Astra (via Codex) | 10/10, fastest | 5 (1 no one else found) | judge | plan quota | **Reviewer** |
| DeepSeek V4 Flash | 10/10 | **9** | 43 | **~$1.50** | **Bulk worker, reviewer** |
| Gemini 3.8 Flash (High) | 9/10 | 5 | 35 | $0 (plan quota) | Free drafts and reads |
| Grok 4.7 (OpenRouter) | 6/6 short only | 7 | not run | $4.06 for 6 short | Second-best reviewer; found nothing the others missed |
| Grok 4.7 (Grok Build CLI) | 10/10 | 2 of 3 on case 1 (only case run) | not run | Grok plan quota (~77% of a week's allowance) | Second reviewer beside Codex; personal folders only |
| Kimi K3 | 10/10, slowest | 4 | 45 | ~$14.70 | Backup only |
| DeepSeek V4 Pro | 7/10 | not run | not run | ~$1.50 | Dropped: its CLI stopped partway twice |
| Grok 4.6 | 9/9 | not run | not run | ~$9 | Second opinions |
| GLM-5.3 | 9/10 | not run | not run | ~$24 | Second opinions; slow and costly on long tasks |
| MiniMax M3 | 7/9 | not run | not run | ~$4 | Second opinions |

## History: roles on 22 Sep (before Opus 5.5)

| Role | Then | Now |
|---|---|---|
| Main worker | Opus 5 | Opus 5.5 |
| Final reviewer | Fable 5.1 | Opus 5.5 high |
| Review trio | Astra + Fable + DeepSeek Flash (together they found every bug any reviewer found) | Astra + Opus 5.5 + DeepSeek Flash |

## Extra models via OpenRouter (first look only)

Credit ran out before the long tasks. Two review cases is a small sample.

| Model | Short tasks | Review bugs (cases 1-2) | Cost for 4 tasks | Time for 4 |
|---|---|---|---|---|
| Hunyuan 4 | 4/4 | 3 | $0.65 | 36 min |
| Qwen 3.8 Max | 4/4 | 3 | $1.90 | 42 min |
| Sakana Fugu Max | 4/4 | 1 | $1.47 | **12 min** |
| DeepSeek V4.1 Flash | 4/4 | tool error | $0.10 | 21 min |
| MiMo v2.6 Pro | 3/4 | 0 | $1.54 | 84 min |

- Costs are what OpenCode reports; the real OpenRouter charge was about 1.5-2x that.

## Lessons

- The tasks were too easy to separate the top models on pass rate. Speed, cost and code quality were the real differences.
- Every model missed Firestore security-rules bugs: 4 of 5 known rules bugs went unfound. Rules still need a human.
- DeepSeek Flash passed the tests but left 34-54% more defects than Claude (43 vs 28-32). Always follow its code with a review pass.
- A newer model can beat an older, pricier one on review: Opus 5.5 found more bugs than Fable at about a third of the cost.
- Harness traps: the effort gate read the wrong config for Codex; paths starting with `_` broke git's exclude filter;
  a prepaid plan ran out in a day; runs that died on an empty balance (HTTP 402) were voided, not counted as fails.

## Caveats

- One app, one owner, 10 tasks, each run once per model (the Opus 5.5 vs Fable review check ran twice). Treat the ranking as a starting point, then run your own (`bakeoff/`).
- Model names and prices are as of Sep 2026.
