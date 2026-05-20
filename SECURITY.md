# Security

Thanks for taking the time to look at this. Security reports help keep
the project useful for everyone who learns from it.

## Reporting a vulnerability

If you find a security issue — credential leak in git history, missing
authentication on an endpoint, injection vector, broken authorization,
anything that could be abused — please **don't** open a public issue.

Instead, email **`joaoottavioc@gmail.com`** with:

- A brief description of the issue
- A reproduction (curl/code/screenshots) if you have one
- The branch/commit you observed it on (if applicable)

You'll get an acknowledgement within 72 hours. Honest reports get an
honest response — including credit in the fix commit if you'd like one.

## Scope

This is a portfolio / case-study repository. The deployment at
`dev-api.zenbotz.com.br` and `zenbotz.com.br/<slug>` runs the same
codebase. Either is in scope.

**Out of scope:**

- Findings against third-party services we depend on (AWS, OpenAI,
  Groq, Mercado Pago, Meta) — report those upstream.
- Denial of service via rate limits — the existing rate-limit middleware
  is intentional; if you can defeat it that *is* a finding.
- Social engineering, physical security, phishing.

## What I'll do

For confirmed issues:

1. Acknowledge within 72 hours.
2. Triage severity (CVSS-ish: blocker / high / medium / low).
3. Fix on a private branch, then push.
4. Credit you (if desired) in the fix commit + a short note in
   `CHANGELOG.md` once that exists.

For the most common class — credentials accidentally committed —
the response is:
- Rotate the credential at the source (AWS / OpenAI / etc.).
- Scrub history with `git filter-repo` and force-push.
- Update `.gitignore` to prevent the same surface from regressing.

## What's already public

This repo is a portfolio piece. The following are *intentionally* public:

- AWS account ID `578761488332` (account IDs are not secrets by AWS's
  own design; they appear in any ARN).
- Route 53 zone ID, ACM cert ARN, dev RDS endpoint hostname.
- Architecture, tech-debt backlogs, plans, eval methodology.
- Contact email and Facebook App ID (App IDs are public by Facebook's
  design).

The following are *not* and never were public:

- Any `.env*` file other than `.env.example`.
- API keys (OpenAI, Groq, Meta, Mercado Pago, Google Maps, AWS keys).
- RDS master password, encryption keys, JWT signing keys.
- Customer data — the public demo at `pizzaria-do-ze` is a seeded bot
  with fictional products and no real customer accounts.

## Hardening posture

For reviewers curious about how the codebase handles security:

- **Auth:** JWT with 24h expiry, bcrypt password hashing, rate-limited
  login (5 req / 5min), password complexity + common-password blocklist.
- **CSRF:** double-submit cookie pattern on state-changing endpoints.
- **Webhooks:** Mercado Pago HMAC-SHA256 signature verification + per-
  order webhook tokens. WhatsApp uses Meta's signed-request flow.
- **Payment tokens:** Fernet-encrypted at rest. Auto-refresh before
  expiry to avoid token-leak windows during refresh races.
- **CORS:** environment-aware. Wildcard only in development; explicit
  origin allowlist required in production. Web widget has a separate
  per-bot allowlist.
- **Rate limiting:** Redis-based, async, fails closed when Redis is
  unreachable.
- **Distributed locking:** per `contact_id` cart-mutation serialization.
- **PII:** structured logging masks sensitive fields; no `print()`
  calls in production code.
- **LLM output:** sanitized (URLs, emails, phone numbers stripped,
  length capped) before reaching customers.
- **Secrets:** AWS SSM Parameter Store in production, `.env` locally,
  Fernet for any value persisted to the application DB.

See `tech_debt/backlog_vulnerabilities.md` for the full security
backlog and resolution status.
