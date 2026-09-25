# Tool trials (Sep 2026)

**Two tools made it in: Bruno for API tests and agent-browser for headless browser checks. Most others cost more than they saved, or bypassed the claims and guardrails hooks.**

Only agent and dev-workflow tools are listed. "Hands-on" means installed and run on 24 Sep 2026 in a throwaway folder.
"Read only" means docs and code were read on 25 Sep 2026, nothing installed.

## Hands-on

| Tool | What it is | Verdict | Why | Integrated where |
|---|---|---|---|---|
| **Bruno** | API client with plain-text request files | **Adopted** | Agent-written `.bru` files ran first try; a broken check exits 1; JUnit reports for CI; MIT | Library skill `api-testing-bruno` (loaded on demand, not in `_core`) |
| **Vercel agent-browser** | Headless browser CLI for agents | **Adopted, narrowly** | A login flow worked first try; page reads were 72-82% smaller than Playwright's | Library skill `agent-browser`, for subagents, Codex and dsh. A logged-in browser extension stays for your own Chrome. End runs with `close --all` |
| GitButler | Virtual branches in one folder | Skipped | Two agents on two branches in one folder worked, but each saw the other's half-done edits, and it fails inside worktrees. Claims + worktrees already cover this | - |
| Understand-Anything | LLM-built map of a codebase | Skipped | $6.28 and 12 min for one small backend, with one invented claim. Maybe a one-off to onboard someone else | - |
| LLM Council | Several models answer, then vote | Skipped | Same right answers as DeepSeek Flash alone, at 40-400x the cost and 5-40x the time | - |
| context-mode | Runs commands in a sandbox to shrink output | Skipped | Cost more (11 turns vs 6), and commands run through it are invisible to the claims and guardrails hooks | - |
| Yaak | API client | Skipped | Requests live in a database, a 404 still exits 0, paid for commercial use | Bruno instead |
| LiteLLM | One proxy for many model APIs | Skipped | Only 3 of 7 model CLIs could route through it; 300-670 MB idle | `second_opinion.py` routes instead |
| Langfuse | LLM tracing | Skipped | 6 containers; docs want 16 GB RAM | `usage_report.py` reads transcripts instead |
| Code-graph context tool (MCP) | Indexes a repo for the model | Dropped | $0.47 per question vs $0.43 without, same correctness ([routing](routing.md)). Its MCP entry later turned out to point at a deleted folder | - |

## Read only

| Tool or idea | Verdict | Why | Status |
|---|---|---|---|
| DeepSeek V4 Flash image input | **Adopt** | It now reads JPEG, PNG, GIF and WebP (up to ~1,024 tokens each), so cheap vision needs no converter | Point image work at `dsh:deepseek-flash`; check that `dsh` passes images through |
| crawl4ai + markitdown | **Adopt** as the one scraping and conversion stack | Local, free, and both output markdown a model can read | Not wired in yet |
| firecrawl, browser-use, crawlee, scrapy | Skip | Covered by crawl4ai + markitdown, or hosted | - |
| Hermes vs OpenClaw (always-on agents) | **Hermes**, on a spare always-on machine, on direct `deepseek-flash` | They overlap almost fully. Their value is jobs you trigger from your phone (notes, cron, voice), not coding | Keep them off repos and private notes: they sit outside claims and guardrails. Have them write to their own folder |
| gstack | Borrow the idea | Ordered role commands (e.g. `/ship`, `/review`, `/retro`), each running claims, then review, then queue | Idea, not built |
| Review receipts tied to the diff hash | Borrow the idea | Turns "outside review before done" from trusted into enforced | Idea, not built |
| shadcn/improve | Borrow the idea | Plans stamped with a commit, holding the check commands to re-run. The expensive model plans once and a cheap one executes, which only pays on big changes | Close to the mixed-model flow in [token-economy](token-economy.md) |
| gastown | Borrow the idea | A merge queue | `queues.py` covers deploys; a merge target is one `queues.py join` away |
| octomux, agent-deck | Borrow the idea (agent-deck: try) | One inbox for every session | `claims.py list` and `session_mirror.py` cover part of it |
| MartinLoop | Borrow the idea | A hard spend cap per session | Not built; `usage_report.py` only reports |
| claude-squad | Try | Parallel agent sessions in one view | Not tried yet |
| n8n, dify, langflow, OpenHands | Skip | Licence, weight, or overlap with what is here | - |
| ollama | Skip | RAM cost next to several agent sessions | - |
| claude-smart, Graphify | Skip (not tested) | Overlap the memory recall and skill library; Graphify is the same idea as Understand-Anything | - |

## Takeaways

- A tool that runs commands out of sight of the hooks is a no, however much it saves: it breaks claims and guardrails for every other session.
- Check the cost per task, not the pitch. Both "save tokens" tools tried (context-mode, the code-graph tool) cost more than doing without.
- Borrow ideas freely; install little.
