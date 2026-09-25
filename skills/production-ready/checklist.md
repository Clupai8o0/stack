# Production-ready checklist

Each item has a tier. Pick the tier that fits the project, then check every item at that tier and below.

| Tier | When | Example |
|---|---|---|
| **1 Launch** | Anything real people use | Portfolio site, side app, internal tool |
| **2 Users** | People log in, pay, or trust you with data | SaaS, mobile app, marketplace |
| **3 Scale** | Teams, enterprise buyers, or many tenants | B2B SaaS, platform, API product |

Tags show which kind of project an item applies to: `web` site or web app, `api` backend or API, `mobile` app,
`cli` tool or library, `ai` LLM features, `saas` multi-user product. No tag = every project.

## 1. Security

Then run the matching lists in [security-lists.md](security-lists.md): OWASP Top 10, API Top 10, CWE Top 25, MASVS,
LLM Top 10, and ATT&CK for logging.

- [ ] **1** No secrets in the repo or git history (gitleaks / trufflehog in CI); secrets in env or a secrets manager
- [ ] **1** Dependencies scanned (`npm audit` / osv-scanner) and auto-updated (Dependabot / Renovate)
- [ ] **1** `web` HTTPS everywhere, HSTS, secure cookies (`HttpOnly`, `Secure`, `SameSite`)
- [ ] **1** `web` Security headers: CSP, `X-Content-Type-Options`, `Referrer-Policy`, frame protection
- [ ] **1** All input validated on the server (schema: zod / pydantic); output encoded; parameterised queries
- [ ] **1** `api` Rate limits on auth, signup, password reset and any costly endpoint
- [ ] **1** Database access rules tested (Firestore rules tests, Postgres RLS, or per-query owner checks)
- [ ] **2** `web` CSRF protection on cookie-authenticated writes; CORS allowlist, never `*` with credentials
- [ ] **2** File uploads: type and size limits, stored outside the web root, scanned or re-encoded
- [ ] **2** `web` `/.well-known/security.txt` with a contact for vulnerability reports
- [ ] **2** OWASP ZAP (web) or MobSF (mobile) scan on staging; OWASP ASVS / MASVS level 1 reviewed
- [ ] **2** `ai` Prompt-injection guard: model output never runs as code or SQL; tools least-privilege
- [ ] **3** Pen test or bug bounty; threat model written for auth, payments and data export
- [ ] **3** Signed builds / SBOM; secret rotation schedule; key management (KMS)

## 2. Auth and access

- [ ] **1** Passwords hashed (argon2 / bcrypt) or delegated to a provider (Auth.js, Better Auth, Clerk, Supabase, Firebase)
- [ ] **1** Every endpoint checks *who* and *may they* (authorisation), not just logged in
- [ ] **2** Email verification, password reset with expiring single-use tokens, session revoke on password change
- [ ] **2** MFA available; account lockout or backoff on repeated failures
- [ ] **2** Roles and permissions (RBAC) in one place, not scattered `if admin`
- [ ] **3** `saas` SSO: OAuth2 / OIDC and SAML; SCIM provisioning and deprovisioning
- [ ] **3** `saas` Audit log of who did what, when, from where; admin impersonation is logged and visible
- [ ] **3** `api` Scoped API keys / personal access tokens with expiry and revoke

## 3. Data

- [ ] **1** Automated backups, and a restore actually tested (write down how long it took)
- [ ] **1** Schema changes through migrations in the repo, never by hand in production
- [ ] **2** Soft delete or a recycle bin for user-created content
- [ ] **2** Data retention rules: what is kept, for how long, and a job that deletes the rest
- [ ] **2** Point-in-time recovery on the main database
- [ ] **3** `saas` Tenant isolation enforced in the data layer (RLS or tenant-scoped repositories), tested
- [ ] **3** Encryption at rest for sensitive fields; separate keys per tenant if buyers ask
- [ ] **3** Disaster recovery plan with a target: how much data you may lose (RPO), how long you may be down (RTO)

## 4. Reliability

- [ ] **1** Timeouts on every outbound call; retries with backoff and jitter only on safe (idempotent) calls
- [ ] **1** Friendly error pages and messages; no stack traces shown to users
- [ ] **2** `api` Idempotency keys on create and payment endpoints
- [ ] **2** Background jobs in a queue with retries and a dead-letter queue, not in the request
- [ ] **2** Graceful shutdown: finish in-flight requests and jobs on deploy
- [ ] **2** Webhooks received: signature verified, timestamp checked, processed once (idempotent), stored before handling
- [ ] **2** Webhooks sent: signed, retried with backoff, delivery log, replay button
- [ ] **3** Circuit breakers or fallbacks for flaky third parties; feature degrades, app stays up
- [ ] **3** Load test against the expected peak ×2; know the first thing that breaks

## 5. Observability

- [ ] **1** Error tracking (Sentry or similar) on frontend and backend, with release tags
- [ ] **1** Uptime check on the public URL (Uptime Kuma, Better Stack) that alerts your phone
- [ ] **2** Structured JSON logs with a request ID carried across services; no secrets or PII in logs
- [ ] **2** Product analytics on the key funnel (PostHog / Plausible), consent-aware
- [ ] **2** Alerts on error rate and latency, not just "down"
- [ ] **3** Metrics (RED: rate, errors, duration) and distributed tracing (OpenTelemetry)
- [ ] **3** SLOs written down, with an error budget; public status page

## 6. Standard endpoints and files

- [ ] **1** `api` `GET /health` (liveness: process up) — no auth, no dependencies
- [ ] **1** `web` `robots.txt`, `sitemap.xml`, favicon set, `manifest.webmanifest`, custom 404 and 500 pages
- [ ] **2** `api` `GET /ready` (readiness: database and queues reachable) for the load balancer
- [ ] **2** `api` `GET /version` (commit SHA, build time) so you know what is deployed
- [ ] **2** `api` OpenAPI spec served at `/openapi.json` with docs at `/docs`
- [ ] **2** Account endpoints: export my data, delete my account, change email, unsubscribe (one click)
- [ ] **2** `api` Consistent errors (RFC 9457 problem+json), pagination (cursor), filtering, `Retry-After` and rate-limit headers
- [ ] **3** `api` Versioned API (`/v1`) with a deprecation policy and `Sunset` headers
- [ ] **3** `api` `/metrics` (Prometheus) behind auth; webhooks management endpoints (list, test, replay)
- [ ] **3** `saas` Admin endpoints and console: find user, impersonate (logged), refund, feature flags

## 7. Performance

- [ ] **1** `web` Core Web Vitals green on mobile (LCP < 2.5 s, INP < 200 ms, CLS < 0.1)
- [ ] **1** Images resized and in modern formats; static assets on a CDN with long cache headers
- [ ] **2** Database indexes for every frequent query; N+1 queries removed; slow-query log on
- [ ] **2** Caching where reads dominate (HTTP cache, Redis), with a clear invalidation rule
- [ ] **2** `mobile` Cold start and app size tracked; offline and poor-network states handled
- [ ] **3** Performance budgets enforced in CI (bundle size, Lighthouse)

## 8. Deploy and operations

- [ ] **1** One-command, repeatable deploy from CI; nobody deploys from a laptop
- [ ] **1** Separate dev, staging and production, with separate data and keys
- [ ] **1** Environment variables validated at startup; the app refuses to boot with bad config
- [ ] **2** Rollback in one step (previous build or image); database migrations backward compatible
- [ ] **2** Preview deploys per pull request
- [ ] **2** Feature flags for risky launches
- [ ] **2** Infrastructure as code (Terraform, Pulumi, Docker Compose committed), not click-ops
- [ ] **3** Zero-downtime deploys (blue-green or rolling); canary release with automatic rollback
- [ ] **3** Runbooks for the top 5 failures; on-call rota; incident template and post-mortems

## 9. Testing and quality

- [ ] **1** CI runs lint, type check and tests on every pull request; main is protected
- [ ] **1** Tests on the money paths: signup, login, payment, the core action
- [ ] **2** End-to-end tests on staging (Playwright / Maestro) for the key flows
- [ ] **2** `api` Contract tests or API collection (Bruno) run in CI
- [ ] **2** `ai` Eval set of real cases with expected outputs, run before prompt or model changes
- [ ] **3** Test data factories and seeded staging; chaos or failure-injection tests on dependencies

## 10. Privacy and legal

- [ ] **1** Privacy policy and terms that match what the app actually does
- [ ] **1** `web` Cookie consent where required; analytics that respect it
- [ ] **2** Data map: what personal data you hold, where, why, and who processes it (sub-processor list)
- [ ] **2** Emails: SPF, DKIM and DMARC set; unsubscribe link; consent recorded (Spam Act, CAN-SPAM)
- [ ] **2** PII redaction before logs, analytics and LLM calls
- [ ] **3** GDPR / Australian Privacy Act requests handled in the promised time; DPA ready for buyers
- [ ] **3** SOC 2 style controls: access reviews, change management, vendor list, security training

## 11. Accessibility and UX

- [ ] **1** `web` Keyboard navigation, visible focus, alt text, labels on inputs, colour contrast AA
- [ ] **1** Loading, empty and error states for every screen
- [ ] **2** `web` axe or Lighthouse accessibility check in CI; screen-reader pass on key flows
- [ ] **2** `mobile` Dynamic type, dark mode, and platform back behaviour
- [ ] **3** Internationalisation ready (strings extracted, dates, currency, right-to-left if needed)

## 12. Billing and money

- [ ] **2** `saas` Payments through a provider (Stripe); webhooks drive state, never the redirect page
- [ ] **2** `saas` Plans and entitlements checked on the server; trials and cancellations tested
- [ ] **2** Invoices and receipts emailed; tax (GST / VAT) handled by the provider
- [ ] **3** `saas` Usage metering per tenant (API calls, seats, storage) feeding usage-based billing
- [ ] **3** Dunning (failed-payment retries and emails); refunds and credits from the admin console

## 13. Onboarding, support and docs

- [ ] **1** README: what it is, how to run it locally, how to deploy
- [ ] **2** In-app onboarding checklist; time-to-first-value measured
- [ ] **2** Transactional emails (welcome, reset, receipts) through a provider with templates
- [ ] **2** A support channel and a way for users to report bugs from inside the app
- [ ] **3** Public docs and changelog; architecture decision records (ADRs) for big choices
- [ ] **3** `saas` Customer health view: usage, adoption, errors per tenant; SLA alerts

## 14. AI features

- [ ] **2** `ai` Cost per request tracked; hard spend cap and per-user rate limit
- [ ] **2** `ai` Timeouts, streaming, and a fallback model or graceful "try again"
- [ ] **2** `ai` Inputs and outputs logged with PII redacted, for debugging and evals
- [ ] **3** `ai` RAG with permission filtering (users only retrieve what they may read), citations, audit log
- [ ] **3** `ai` Human review path for high-stakes outputs; model and prompt versions recorded per answer

## 15. Mobile specifics

- [ ] **1** `mobile` Crash reporting; minimum OS versions set; store listing, privacy labels filled
- [ ] **2** `mobile` Forced-update / minimum-version check from the server
- [ ] **2** `mobile` Over-the-air updates for JS (EAS Update / CodePush) with a rollback plan
- [ ] **2** `mobile` Deep links and push notifications tested on real devices; tokens refreshed
- [ ] **3** `mobile` Certificate pinning where the risk warrants it; jailbreak/root awareness

## 16. CLI tools and libraries

- [ ] **1** `cli` `--help`, `--version`, non-zero exit codes on failure, no secrets in output
- [ ] **2** `cli` Semantic versioning, changelog, signed releases; install in one line
- [ ] **2** `cli` Config file plus env vars; `--dry-run` for anything destructive
- [ ] **3** `cli` Self-update or upgrade command; telemetry opt-in only
