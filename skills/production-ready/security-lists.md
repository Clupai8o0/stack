# Security bug-list sweep

The standard security lists, turned into checks. Run the lists that match the project kind, after
[checklist.md](checklist.md) section 1. Each check says where to look for evidence in the repo.

| List | Run it on | Link |
|---|---|---|
| OWASP Top 10 (2025) | every `web` and `api` project | https://owasp.org/Top10/ |
| OWASP API Security Top 10 (2023) | every `api` | https://owasp.org/API-Security/ |
| CWE Top 25 | all code, per feature in review | https://cwe.mitre.org/top25/ |
| OWASP MASVS | every `mobile` app | https://mas.owasp.org/MASVS/ |
| OWASP LLM Top 10 (2025) | every `ai` feature | https://genai.owasp.org/llm-top-10/ |
| OWASP ASVS | tier 2+ `web` / `api`: the full requirement list | https://owasp.org/www-project-application-security-verification-standard/ |
| MITRE ATT&CK | tier 2+: what to log and alert on | https://attack.mitre.org/ |

Tiers and tags work as in the checklist. Mark each check Done (with the file), Missing, Unknown or N/A.

## OWASP Top 10 (2025)

- [ ] **1** **A01 Broken access control:** every read and write checks the object belongs to the caller (no IDOR:
  changing an ID in the URL shows someone else's data); deny by default; SSRF blocked (server-side fetches of
  user-given URLs use an allowlist and refuse private IPs and cloud metadata `169.254.169.254`)
- [ ] **1** **A02 Misconfiguration:** debug mode off in prod; default accounts and sample routes removed; no
  directory listing; storage buckets private; CORS and headers set (see checklist section 1)
- [ ] **1** **A03 Supply chain:** lockfile committed; dependency scan in CI; no packages from unknown authors
  added without a look; GitHub Actions pinned to a version or SHA
- [ ] **1** **A04 Crypto failures:** TLS everywhere; passwords hashed with argon2 / bcrypt; no home-made crypto,
  MD5 or SHA1 for security; tokens from a secure random source
- [ ] **1** **A05 Injection:** parameterised SQL / ORM only; no user input in shell commands, `eval`, templates or
  NoSQL query objects; output encoded (no `dangerouslySetInnerHTML` / `innerHTML` with user data) to stop XSS
- [ ] **2** **A06 Insecure design:** abuse cases written for money, invites and sharing flows (what does a
  malicious user try?); limits on anything free or costly
- [ ] **1** **A07 Auth failures:** rate limit and backoff on login; no user enumeration in login or reset
  messages; sessions expire and rotate on login; MFA at tier 2
- [ ] **2** **A08 Integrity failures:** no unsafe deserialisation (`pickle`, `yaml.load`, Java serialisation) of
  untrusted data; webhook and update payloads signature-checked; CI can't be changed by a fork's pull request
- [ ] **2** **A09 Logging and alerting:** security events logged (see ATT&CK below) and someone is alerted
- [ ] **1** **A10 Exceptional conditions:** errors fail closed (an exception in an auth check denies, never
  allows); no stack traces to users; timeouts and limits on every input

## OWASP API Security Top 10 (2023)

- [ ] **1** `api` **API1 Object-level auth (BOLA):** every `/things/:id` route checks ownership, tested with a second user
- [ ] **1** `api` **API2 Broken auth:** tokens validated (signature, expiry, audience); no API keys in URLs
- [ ] **1** `api` **API3 Property-level auth:** no mass assignment (request body can't set `role`, `ownerId`,
  `price`; use an allowlist schema); responses never return fields the caller shouldn't see (password hash, other users' emails)
- [ ] **1** `api` **API4 Resource consumption:** page size capped; body size, upload size and query depth limited; rate limits
- [ ] **1** `api` **API5 Function-level auth:** admin routes check the role on the server, not just hidden in the UI
- [ ] **2** `api` **API6 Business flows:** bot-sensitive flows (signup, checkout, invites, votes) have limits or CAPTCHA
- [ ] **1** `api` **API7 SSRF:** see A01
- [ ] **1** `api` **API8 Misconfiguration:** see A02; verbose errors off; unused HTTP methods rejected
- [ ] **2** `api` **API9 Inventory:** old API versions and staging hosts listed and shut down; no forgotten endpoints
- [ ] **2** `api` **API10 Unsafe third-party APIs:** responses from other APIs validated like user input; timeouts set

## CWE Top 25 (grouped)

Check these on every feature in code review. Most map to the OWASP items above; the ones below are the extras.

- [ ] **1** **Path traversal (CWE-22):** file paths built from user input are normalised and kept inside one folder
- [ ] **1** **Unrestricted upload (CWE-434):** type checked by content, not extension; never served from an executable path
- [ ] **1** **CSRF (CWE-352):** see checklist section 1
- [ ] **1** **Hard-coded credentials (CWE-798):** none in code, config, mobile bundles or front-end JS
- [ ] **1** **Sensitive data exposure (CWE-200):** no secrets, tokens or PII in logs, errors, URLs or analytics
- [ ] **1** **Missing auth on a critical function (CWE-306):** delete, export, payment and admin actions all require auth
- [ ] **2** **Resource exhaustion (CWE-400):** regexes safe from ReDoS; loops and recursion bounded by input size
- [ ] **2** `cli` **Memory safety (CWE-787, 125, 416, 476, 190):** only for C, C++ or `unsafe` Rust: fuzz and run sanitizers

## OWASP MASVS (mobile)

- [ ] **1** `mobile` **STORAGE:** tokens in Keychain / Keystore (SecureStore), never AsyncStorage or plain prefs; no secrets in the bundle
- [ ] **1** `mobile` **NETWORK:** HTTPS only (no cleartext exceptions in `Info.plist` / network config)
- [ ] **1** `mobile` **AUTH:** the server enforces every check; the app hiding a button is not security
- [ ] **2** `mobile` **PLATFORM:** deep links and intents validate input; WebViews don't load untrusted URLs with JS bridges
- [ ] **2** `mobile` **CODE:** debug builds and logging stripped from release; min OS and dependencies current
- [ ] **2** `mobile` **PRIVACY:** permissions asked only when needed; store privacy labels match what the app collects
- [ ] **3** `mobile` **CRYPTO / RESILIENCE:** platform crypto only; pinning and tamper checks where the risk warrants it

## OWASP LLM Top 10 (2025)

- [ ] **2** `ai` **LLM01 Prompt injection:** text from users, web pages, files or emails is treated as data, never instructions; tools gated
- [ ] **2** `ai` **LLM02 Sensitive info disclosure:** no secrets or other users' data in prompts; output checked before display
- [ ] **2** `ai` **LLM05 Improper output handling:** model output escaped before HTML, never run as code, SQL or shell
- [ ] **2** `ai` **LLM06 Excessive agency:** tools least-privilege; destructive or paid actions need user confirmation
- [ ] **2** `ai` **LLM07 System prompt leakage:** nothing secret in the system prompt; assume users can read it
- [ ] **2** `ai` **LLM10 Unbounded consumption:** token caps, per-user limits and a spend cap (see checklist section 14)
- [ ] **3** `ai` **LLM03 / 04 Supply chain and poisoning:** models and datasets from trusted sources; RAG inputs reviewed
- [ ] **3** `ai` **LLM08 Vector store:** retrieval filtered by the caller's permissions; tenants never share an index unfiltered
- [ ] **3** `ai` **LLM09 Misinformation:** citations shown; high-stakes answers have a human check

## MITRE ATT&CK: what to log and alert on

ATT&CK lists what attackers do once they are in. For an app, turn the common techniques into log events and alerts.

- [ ] **2** **Valid accounts / brute force (T1078, T1110):** log logins and failures with IP; alert on bursts or credential stuffing
- [ ] **2** **Account manipulation (T1098):** log role changes, new API keys, MFA removed, email changed; notify the user
- [ ] **2** **Exploit public-facing app (T1190):** error spikes and WAF / rate-limit hits alert someone
- [ ] **3** **Data from cloud storage / exfiltration (T1530, T1567):** log bulk exports and large downloads; alert on unusual volume
- [ ] **3** **Supply chain compromise (T1195):** alert on new dependencies and CI config changes in review
- [ ] **3** **Account removal / impact (T1531, T1485):** mass deletes need confirmation and are logged and reversible
