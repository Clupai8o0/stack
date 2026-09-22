# Model bake-off (Sep 2026)

**Keep Opus 5, Fable 5.1, GPT-6 Astra (Codex) and DeepSeek V4 Flash. Kimi K3 is backup only. DeepSeek V4 Pro and OpenCode Go were dropped.**

All work came from the git history of a production React Native + Firebase app. The runner is in `bakeoff/`.

## What was tested

| Test | What |
|---|---|
| Coding | 10 real tasks from git history: 6 short, 4 long (one touched 20 files). Each was checked to fail before the real commit and pass after it. |
| Review | 5 real changes whose tests passed, containing 17 bugs a later review caught. Graded blind. |
| Code quality | Astra reviewed each model's passing code on the 2 long backend tasks, blind. |

## Results

| Model | Coding | Review (of 17 bugs) | Code quality (defects, lower is better) | Cost for 10 tasks | Role |
|---|---|---|---|---|---|
| Opus 5 | 10/10 | not run | 32 | $20.60 | **Main worker** |
| Fable 5.1 | 10/10 | 6 | **28** | $26.95 | **Hard tasks, final review** |
| GPT-6 Astra (via Codex) | 10/10, fastest | 5 (1 no one else found) | judge | plan quota | **Reviewer** |
| DeepSeek V4 Flash | 10/10 | **9** (most of the models tested; Opus not run) | 43 | **~$1.50** | **Bulk worker, reviewer** |
| Gemini 3.8 Flash (High) | 9/10 | 5 | 35 | $0 (plan quota) | Free drafts and reads |
| Grok 4.7 (OpenRouter) | 6/6 short only | 7 | not run | $4.06 for 6 short | Second-best reviewer; found nothing the others missed |
| Grok 4.7 (Grok Build CLI) | 10/10 | 2 of 3 on case 1 (only case run) | not run | Grok plan quota (~77% of a week's allowance) | Second reviewer beside Codex; personal folders only |
| Kimi K3 | 10/10, slowest | 4 | 45 | ~$14.70 | Backup only |
| DeepSeek V4 Pro | 7/10 | not run | not run | ~$1.50 | Dropped: its CLI stopped partway twice |
| Grok 4.6 | 9/9 | not run | not run | ~$9 | Second opinions |
| GLM-5.3 | 9/10 | not run | not run | ~$24 | Second opinions; slow and costly on long tasks |
| MiniMax M3 | 7/9 | not run | not run | ~$4 | Second opinions |

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
- The three reviewers GPT-6 Astra + Fable + DeepSeek Flash together found every bug any reviewer found.
- Harness traps: the effort gate read the wrong config for Codex; paths starting with `_` broke git's exclude filter;
  a prepaid plan ran out in a day; runs that died on an empty balance (HTTP 402) were voided, not counted as fails.

## Caveats

- One app, one owner, 10 tasks, each run once per model. Treat the ranking as a starting point, then run your own (`bakeoff/`).
- Model names and prices are as of Sep 2026.
