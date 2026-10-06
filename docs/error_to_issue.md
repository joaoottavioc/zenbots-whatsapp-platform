# Production errors → GitHub issues

How an exception in the backend or worker becomes a GitHub issue an agent (or a
human) can fix. Code side: `app/sentry_setup.py` + `app/pii.py`. Agent side: the
"Runbook: fixing a production error" section of `CLAUDE.md`.

```
exception / logger.error(...)  ──►  Sentry event (scrubbed, tagged)
                                        │  alert rule: "A new issue is created"
                                        ▼
                              GitHub issue  [bug] [agent-ready]
                                        │  human comments "@claude fix this"
                                        ▼
                              claude.yml → PR to develop → human review
```

## What the app sends

- **Off unless `SENTRY_DSN` is set.** Nothing is imported or sent otherwise.
- Captured: unhandled exceptions (FastAPI requests, ARQ jobs) **and every
  `logger.error(...)` record**. The WhatsApp and web message handlers catch
  their own exceptions and log them with `exc_info=True`, so this is how most
  message-processing failures reach Sentry. `INFO`+ records become breadcrumbs.
- Tags on every event: `trace_id`, `bot_id`, `contact_id`, `channel`,
  `component` (`backend` / `worker`), plus Sentry's `environment` (from
  `ENVIRONMENT`) and `release` (from `SENTRY_RELEASE`, optional).
- **Never sent:** local variables of stack frames, request bodies, cookies,
  non-allowlisted headers, the `user` block. Phone numbers are masked
  (`5511***00`); CEPs, emails, bearer tokens and inline `token=...` values are
  replaced; values under content/address/name/credential keys become
  `[Filtered]`. If scrubbing ever raises, the event is **dropped**, not sent raw.
  Tests: `tests/test_sentry_setup.py`, `tests/test_pii_error_handler.py`.

## One-time setup (you, not an agent)

### 1. Sentry project
1. Create a Sentry organization (the free Developer plan is enough to start)
   and a **Python → FastAPI** project named `zenbots-backend`. One project for
   both processes; the `component` tag tells them apart.
2. Copy the **DSN** (Project Settings → Client Keys).
3. Project Settings → **Security & Privacy**: keep *Data Scrubber* and *Use
   Default Scrubbers* on, and turn on *Prevent Storing of IP Addresses*. This is
   a second layer on top of the app-side scrubbing.

### 2. Get the DSN into the running tasks
The DSN is not a secret in the cryptographic sense (it only allows sending
events), but keep it out of git anyway.

- **Local:** add `SENTRY_DSN=...` to `.env` (see `.env.example`), then
  `docker compose restart backend worker`.
- **AWS dev/prod:** this needs an `infra/` change, which agents must not make.
  1. Add `sentry/dsn` to the parameter map passed to `modules/ssm-parameters`
     in `infra/environments/dev` (the module names parameters
     `/${project}/${environment}/<key>`), apply, then set the real value:
     `aws ssm put-parameter --name /zenbots/dev/sentry/dsn --type SecureString --overwrite --value '<dsn>'`.
  2. Add `SENTRY_DSN` to the backend **and** worker `secrets` blocks of the ECS
     task definitions (`infra/environments/dev/main.tf`, later `prod/`), and
     optionally `SENTRY_RELEASE` = the image tag in `environment`.
  3. Apply Terraform (local apply or the `terraform.yml` workflow_dispatch), then
     redeploy so new tasks pick it up.

### 3. Verify events arrive and are clean
From the backend container, with `SENTRY_DSN` set:
```bash
docker compose exec backend python -c "
from app.sentry_setup import init_sentry; init_sentry('worker')
import logging, sentry_sdk
from app.context import new_trace_id, current_bot_id
new_trace_id(); current_bot_id.set(1)
logging.getLogger('app.smoke').error('sentry smoke test to=5511999990000 access_token=abc123')
sentry_sdk.flush()"
```
In Sentry, open the event and check: tags `trace_id`, `bot_id=1`,
`component=worker`, `environment`; the message shows `5511***00` and
`access_token=[Filtered]`.

### 4. GitHub integration + alert rule
1. Sentry → **Settings → Integrations → GitHub → Install**. Grant it access to
   `joaoottavioc/zenbots-whatsapp-platform` only.
2. Make sure the labels exist in the repo:
   `gh label create bug --force` and `gh label create agent-ready --color 0e8a16 --force`.
3. Sentry → **Alerts → Create Alert → Issues**, project `zenbots-backend`:
   - **Environment:** `production` (add a second rule for `dev` only if you want
     dev errors as issues too; it gets noisy).
   - **When:** *A new issue is created*. Optionally add *A resolved issue
     regresses* as a second trigger.
   - **If (filters):** *The issue's category is equal to Error*. To skip
     one-off noise, add *The issue is seen more than 1 time in 1h*, but that
     filter only works with the "event or issue activity" trigger, so start
     without it.
   - **Then:** *Create a new GitHub issue* → repository
     `joaoottavioc/zenbots-whatsapp-platform`, labels `bug`, `agent-ready`,
     assignee empty. (The fields in this panel come from GitHub, so check that
     the labels field appears after the integration is installed.)
   - **Action interval:** 24 hours.
   - Name it `Prod error → GitHub issue`.
4. Trigger the smoke test above against the environment the rule watches, and
   confirm a GitHub issue appears with both labels.

### 5. Getting the trace_id into the issue
The issue Sentry creates always contains a **link to the Sentry issue** and the
stack trace. Sentry doesn't document whether tags are copied into the body, so
check the first real issue:

- **If `trace_id` shows in the issue body:** nothing else to do.
- **If it doesn't:** agents read it from Sentry's API. Create a Sentry
  **internal integration** (Settings → Custom Integrations → New Internal
  Integration) with **Issue & Event: Read** only, and store its token as the
  GitHub secret `SENTRY_AUTH_TOKEN`. Then expose it to `claude.yml` by adding
  `SENTRY_AUTH_TOKEN: ${{ secrets.SENTRY_AUTH_TOKEN }}` to its `env` and
  `Bash(curl -s -H "Authorization: Bearer $SENTRY_AUTH_TOKEN" https://sentry.io/api/0/*)`
  to its `--allowedTools`. The runbook in `CLAUDE.md` shows the call. Either
  way, a human can paste the `trace_id` into the issue.

### 6. Starting the agent
Sentry creates the issue as a GitHub App (a bot). `claude-code-action` ignores
events from bots by default, so the `agent-ready` label Sentry adds does **not**
start Claude. This is deliberate: every production error isn't worth an agent
run, and exception text is untrusted input. To start one, a maintainer:

- reads the issue, and
- comments `@claude fix this — see the runbook in CLAUDE.md`, or removes and
  re-adds the `agent-ready` label.

To make it fully automatic later, add `allowed_bots: "<sentry bot login>"` to
`claude.yml` (use the exact author login shown on the first Sentry-created
issue) and consider a stricter filter in the alert rule.

## Costs and limits
- Sentry Developer plan: 5k errors/month. Errors are only a few per day
  unless something is badly broken, and the 24h action interval caps issue
  spam to one per Sentry issue per day.
- `SENTRY_TRACES_SAMPLE_RATE` defaults to `0` (errors only). Performance
  tracing uses a separate quota; raise it deliberately.
