---
name: token-economy
description: Pick the model, effort and route for any subagent, workflow agent, reviewer or second opinion so work stays high quality at the lowest usage. Use before every Agent or Workflow call, before choosing effort or ultracode, and whenever Codex, Gemini, OpenCode, DeepSeek, GLM, Kimi, Qwen, Grok or MiniMax could take work or a review, or one of them is out of quota.
---

# Token economy

Measured 17 Sep 2026 on the owner's real work: 23 model setups, 5 task types (fact-check,
bug fix, claim audit, PR review with 4 planted defects, mentor update), 2 runs each, blind
judged by Opus 5 and Codex. Full results: `docs/routing.md`.

## 1. Claude subagents: pick model and effort

| Work | Model, effort | Measured |
|---|---|---|
| Default for real work: audits, reviews, fixes, drafting, research | `claude-opus-5`, **low** | 96-100% on every scored task, writing 8.0/10, **$0.33/task** |
| Text a reviewer, client or mentor reads; final judge | `claude-opus-5`, **high** | best writing 8.8/10, $0.53/task |
| Mechanical work where a test or script decides pass/fail | `claude-sonnet-5`, low | 100% when tests decide, $0.24; missed 25% of review defects, invents facts in prose |
| One-line lookups | do it inline, no agent | every agent pays ~30-50k start-up tokens (~$0.15-0.30 on Opus) |

Do not use for subagents: Opus 5 at xhigh/max (2.7x cost, no meaningful gain), Opus 4.8 (worse, 2.3x
cost), Fable 5.1 (no meaningful gain, 2-3x cost), Haiku 4.5 (missed 62% of review defects).

## 2. Rules

1. **Always pass `model` and `effort`** on every workflow `agent()` call. Agents inherit
   the session effort, and ultracode sets xhigh.
2. **Don't do "cheap model executes, Opus reviews" to save money.** Sonnet then an Opus
   review cost $0.78 versus $0.33 for Opus low alone, at the same quality. A review redoes the work.
3. **Fewer, bigger agents.** Never spawn an agent for something one tool call answers.
4. **Keep agents short.** Split any job past ~100 steps. Long agents re-read their whole
   context every step, and 10 long agents were 71% of past Opus 5 usage.
5. **Ultracode only for audits where a miss is costly, or wide fan-out.** Otherwise use
   normal effort and ask for a workflow. Even under ultracode, pin effort per stage.
6. **Independent review goes outside Claude** (section 3). It costs almost no Claude usage.

## 3. Outside models: second opinions, reviews, fallbacks

**Who does what** (owner's call 22 Sep 2026, from a bake-off on a production React Native + Firebase app: 10 coding tasks, a blind review test with 17
real bugs, and a blind code-quality review; results in `docs/bake-off.md`):

| Model | Route | Role | Evidence |
|---|---|---|---|
| Opus 5 | Claude Code | **Main worker** | 10/10 tasks; second-cleanest code; ~25% cheaper than Fable |
| Fable 5.1 | Claude Code | Hardest tasks, final review | 10/10; cleanest code in the blind quality review |
| GPT-6 Astra | `codex` (pass `-c model_reasoning_effort=high`) | **Independent reviewer**, fast long runs | 10/10; 3-5x faster on long tasks; caught the only bug no one else found |
| DeepSeek V4 Flash | `dsh:deepseek-flash` | **Default bulk worker**, China worker, cheap third reviewer | 10/10 for ~$1.50 total; best reviewer (9/17 bugs); code needs a review pass (more defects than Claude) |
| Gemini 3.8 Flash (High) | `agy:gemini-3.8-flash-high` | Free fast drafts and short reads | 8/9 coding, 3-6 min each; 5/17 in review, some confident wrong claims |
| Grok 4.7 | `grok:grok-4.7` (Grok Build, Grok plan quota; personal folders only) | **Second reviewer beside Codex**; stand-in when Codex is out of quota | 7/17 in review (Astra 5); 6/6 short coding via OpenRouter; slower than Astra, found no rules bugs |
| Kimi K3 | `kimi:kimi-k3` | **Backup only**, if DeepSeek is down (China) | 10/10 but slowest, ~10x Flash's cost, most defects in the quality review, 4/17 in review, nothing unique |

Review trio: **Astra + Fable + DeepSeek Flash**; together they found every bug any reviewer found.
Dropped: DeepSeek V4 Pro (7/10; its tool stopped partway twice), OpenCode Go (funds ran out in a day; use direct
keys), OpenCode Zen, Haiku 4.5, MiniMax direct. GLM-5.3, Grok 4.6 and MiniMax M3 go through OpenRouter for second
opinions only (`opencode:openrouter/<vendor>/<model>`, key in `OPENROUTER_API_KEY`).

Call them from Bash, not through a Claude subagent. Only the answer enters the context.

```bash
python3 $CLUPAI_HOME/scripts/second_opinion.py PROMPT_FILE \
  --cwd <dir> [--network] [--confidential] [--ladder codex,dsh:deepseek-flash]
```

It tries each route in order and moves on when one fails or is out of quota. It prints the
answer on stdout and one JSON usage line per attempt on stderr.

| Route | Review (4 planted) | Audit | Writing /10 | $/task* | Time | Best for |
|---|---|---|---|---|---|---|
| Codex (`codex`, ChatGPT plan) | 100% | 100% | judge only | own quota | 100-240s | default second opinion |
| DeepSeek V4 Pro (`opencode-go`) | 100% | 100% | 5.0 | **$0.06** | 180s | cheap review and verification |
| GLM 5.3 (`opencode-go`) | 100% | 100% | **7.5** | $0.27 | 330s | review and writing |
| Kimi K3 (ran on `opencode-go`; now `kimi`) | 100% | 100% | 6.0 | $0.28 | 160s | review, fewest tokens |
| Gemini 3.8 Flash (`agy`) | 100% | 100% | 5.5 | Google quota | 175s | review when others are out |
| Grok 4.6 (`opencode-go`) | 88% | 100% | 7.2 | $0.26 | 195s | writing drafts |
| Qwen 3.8 Max (`opencode-go`) | 88% | 96% | 7.2 | $0.26 | 400s | writing, slow |
| Gemini 3.1 Pro (`agy`) | 88% | 100% | 3.5 | Google quota | 110s | not recommended |
| DeepSeek V4 Flash (ran on `opencode-go`; now `dsh`) | 88% | 100% | 4.0 | $0.02 | 195s | bulk lookups |
| Grok Build 0.1 (`opencode` Zen, dropped) | 75% | 100% | 5.5 | $0.32 | 220s | not recommended |
| MiniMax M3 (`opencode-go`) | 62% | 100% | 4.0 | $0.05 | 105s | not for review |

*List-price equivalent. OpenCode Go is a $10/month plan with caps. Claude Opus 5 low on
the same tasks: review 100%, writing 6.8 in the same judging batch, $0.33, 20-40s.

**DeepSeek goes through `dsh`, not OpenCode** (user's call, 19 Sep 2026). `dsh` is DeepSeek's own CLI
(`npm i -g @deepseek-ai/dsh`) on the direct API with the user's own key. Route `dsh:<model>` (`deepseek-flash`,
`deepseek-v4-pro`; the API's `/models` lists exactly these two, and the old name `deepseek-v4-flash` is an alias). The key is in the keychain (service `deepseek-api`), exported as `DEEPSEEK_API_KEY` by `~/.zprofile`;
the script falls back to the keychain itself. Model is set by `DSH_MODEL`, read by `~/.dsh/profiles/headless/cordis.patch.yml`.
By hand: `DSH_MODEL=deepseek-v4-pro dsh --profile headless "task"`. The benchmark rows above ran DeepSeek on OpenCode Go.
**Kimi goes through `kimi`** (21 Sep 2026): Kimi Code CLI (`~/.kimi-code/bin/kimi`, linked into `~/.local/bin`) on the
Moonshot platform API (`api.moonshot.ai`, not `api.kimi.ai`), key in the keychain as `kimi-api`, exported as
`MOONSHOT_API_KEY` by `~/.zprofile`; the script falls back to the keychain. Provider and models live in
`~/.kimi-code/config.toml` (`api_key_env`, default effort high). By hand:
`kimi -p "task" -m kimi-k3 --output-format stream-json`. Its prompt mode runs every tool without asking (and refuses
`--auto`), so the script always runs it in a whole-disk write sandbox; only `--write` opens `--cwd`.
**OpenCode Go was dropped 22 Sep 2026** (funds ran out within a day of bake-off use). Grok, GLM, Qwen and MiniMax go through
OpenRouter instead: `opencode:openrouter/<vendor>/<model>`, key `OPENROUTER_API_KEY`. The benchmark rows above ran on Go.

**Default ladders** (built into the script):
- review or verification: `codex → dsh:deepseek-flash → agy:gemini-3.8-flash-high → kimi:kimi-k3` (bake-off 22 Sep: Flash found the most real bugs)
- bulk or mechanical work (personal/open code): `dsh:deepseek-flash`, then a review pass; hard or long tasks: Opus or Fable (Kimi only if DeepSeek is down)
- writing second draft: `glm-5.3 → grok-4.6 → qwen3.8-max`, and Claude Opus 5 still writes the final text
- Policy filters the ladder: confidential keeps Codex only, open adds dsh, kimi and OpenCode, personal adds Antigravity (Gemini).
  OpenCode's policy allows using prompts to improve its service, and some upstream providers may train on inputs.

## 3b. Project model policy and delegated coding

The policy comes from the folder: `$CLUPAI_HOME/model-policy.json`.
`second_opinion.py` applies it automatically from `--cwd`. Unlisted folders are confidential.

| Policy | Folders | Coding by | Review by |
|---|---|---|---|
| confidential | client work, private notes, anything unlisted | Claude, Opus 5 low workers | Codex |
| open | open-source and study repos | Claude, or DeepSeek V4 Flash via `dsh` in a disposable worktree (`--write`) | Codex, then DeepSeek V4 Flash |
| personal | your own side projects | DeepSeek V4 Flash via `dsh` (or Gemini via `agy` for quick free drafts) in a worktree; Claude runs the tests and reads the diff | Codex, DeepSeek V4 Flash, Gemini 3.8 Flash |

Coding benchmark (17 Sep 2026): a feature with 31 hidden tests plus 4 planted bugs with 29 tests, 2 runs each.

| Coder | Feature | Bugs | $/task* | Time |
|---|---|---|---|---|
| Claude Opus 5 low | 100% | 100% | 0.39-0.45 | 30-35s |
| Claude Sonnet 5 low | 100% | 100% | 0.18-0.23 | 50s |
| Codex (gpt-5.6-sol) | 100% | 100% | Codex quota | 115-130s |
| DeepSeek V4 Flash | 100% | 100% | **0.01-0.02** | 95-240s |
| DeepSeek V4 Pro | 100% | 100% | 0.05-0.07 | 100-200s |
| MiniMax M3 | 98% | 100% | 0.06-0.12 | 70-210s |
| GLM 5.3 | 100% | 100% | 0.16-0.29 | 100-310s |
| Grok 4.6 | 100% | 100% | 0.27-0.50 | 105-220s |
| Kimi K3 | 100% | 100% | 0.29-0.54 | 85-300s |
| Qwen 3.8 Max | 100% | 100% | 0.28-0.29 | 290-440s |
| Grok Build 0.1 (Zen) | 98% | 100% | 0.35-0.36 | 140-290s |
| Gemini 3.8 Flash | 100% | 100% | 0.55-0.71 (Google quota) | 240-290s |
| Gemini 3.1 Pro | 100% | 100% | 0.99-1.00 (Google quota) | 310s |

*List-price equivalent. Every coder passed these clearly specified tasks, so choose on price, speed and
policy. For open projects: DeepSeek V4 Flash codes, Claude verifies. Code understanding (the PR review
task) is where they differ: DeepSeek V4 Pro, GLM 5.3, Kimi K3 and Gemini 3.8 Flash scored 100%,
Grok 4.6 88%. Outside models are 3-10x slower, so run them in parallel worktrees, not in line.

## 4. Workflow snippet

```js
const WORK  = { model: 'claude-opus-5',   effort: 'low'  }  // default
const JUDGE = { model: 'claude-opus-5',   effort: 'high' }  // sensitive text, final call
const MECH  = { model: 'claude-sonnet-5', effort: 'low'  }  // only when a test decides
await agent(prompt, { ...WORK, label: 'audit:claims' })
```

For an outside reviewer inside a workflow, have one `WORK` agent run `second_opinion.py`
with Bash and return its stdout. Don't use a relay agent per model.

## 5. Caveats

Two runs per setup. Tasks were 3-60 steps, not 300-step jobs. API list price stands in for
plan usage. Re-run the benchmark when models change (harness: `bakeoff/`).
