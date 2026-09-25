---
name: production-ready
description: Audit an app, API, website, mobile app or CLI tool against a production-readiness checklist (security incl. OWASP / CWE / MASVS / LLM Top 10 / ATT&CK lists, auth, data, reliability, observability, standard endpoints, deploy, testing, privacy, billing, AI, mobile) and report what is missing, ranked. Use when the user asks "is this production ready", "what's missing before launch", "make this production ready", "launch checklist", or before shipping a project to real users or a client.
---

# Production-ready audit

Check a project against [checklist.md](checklist.md) and report the gaps that matter for *this* project. Audit
first; change code only when the user asks.

## Steps

1. **Classify the project.** Read the README, package files and folder layout. Decide:
   - kind: `web`, `api`, `mobile`, `cli`, plus `ai` if it calls an LLM and `saas` if it has accounts or tenants;
   - tier: **1 Launch** (real people use it), **2 Users** (logins, payments or personal data),
     **3 Scale** (teams, enterprise buyers, many tenants). If unsure, pick the lower tier and say so.
2. **Filter the checklist.** Keep items at the chosen tier and below whose tags match the project kind
   (untagged items always apply).
   Add the matching lists from [security-lists.md](security-lists.md) (OWASP Top 10, API Top 10, CWE Top 25,
   MASVS, LLM Top 10, ATT&CK logging), filtered the same way. Report their gaps with the rest, in one ranking.
3. **Look for evidence, don't guess.** For each item, search the repo: config files, CI workflows, middleware,
   routes (`/health`, `/ready`, `/version`), rules files, migrations, tests, env handling. Mark each:
   - **Done**: point to the file that proves it.
   - **Missing**: nothing found.
   - **Unknown**: lives outside the repo (dashboard settings, backups, DNS). Ask or list it for the user to confirm.
   - **N/A**: does not apply, with a three-word reason.
4. **Rank the gaps.** Order Missing items by harm: data loss or breach first, then outage, then legal, then
   everything else. Quick wins (under an hour) get flagged.
5. **Report.** Keep it short:
   - one line: tier, kind, and "X of Y done";
   - a table of the top 10 gaps: item, why it matters in one line, effort (S/M/L);
   - the Unknown list as questions;
   - offer the full per-section table on request.
6. **If asked to fix:** work through the ranked list one item per commit, smallest safe change first, with a
   test or a check command for each. Security, auth, database rules and payments changes get an outside review
   before they are called done.

## Notes

- The checklist is a floor, not a goal. Don't push Tier 3 work onto a Tier 1 project; say which items become
  relevant at the next tier instead.
- Never scan or probe systems the user does not own. Security checks run on the user's own repo and staging.
- Projects differ: if the project needs something the checklist lacks, add it to the report and suggest adding
  it to `checklist.md`.
