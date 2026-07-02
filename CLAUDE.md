# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ZenBots** is a SaaS platform that lets restaurant owners deploy AI-powered WhatsApp chatbots for automated order management (cart, payments, delivery). It uses a Python FastAPI backend with OpenAI (gpt-4o-mini) for real-time tool-calling chat and OpenAI (gpt-4o) for menu extraction. Local development uses Ollama (phi3:mini) as an optional LLM backend via the `OPENAI_BASE_URL` env var.

### Current Maturity

The platform is in **active development** with the dev environment live on AWS. Key stats:
- **Test suite**: ~1,891 tests collected (`pytest --collect-only`), ~55% codebase coverage (CI green on Python 3.10; ~9 fail locally on 3.14 due to bcrypt compat)
- **Tech debt**: Tracked across 10 backlog files in `tech_debt/`. Major refactorings complete (God Function split, enum-based state machine, security hardening, observability stack). See `tech_debt/` for full status.
- **Production readiness**: ~45-50% overall. Critical P0 blockers remain in security and data integrity (see `tech_debt/backlog_production.md`).
- **WhatsApp Embedded Signup**: Working in dev mode (Facebook app review required for production)
- **Deployment**: Dev environment **fully operational** on AWS (ECS Fargate + RDS + ElastiCache). Production environment not yet provisioned. Full details in `docs/aws_architecture.md`.
- **Observability**: Structured logging, health checks, per-bot cost tracking, and anomaly detection implemented (Phases 0-3 complete).

## Running the Project

### Local Development (Docker Compose)

```bash
# Start all services (db, redis, ollama, mailhog, backend, worker)
docker compose up --build

# Start in detached mode
docker compose up -d

# View logs
docker compose logs -f backend
docker compose logs -f worker
```

The backend runs at `http://localhost:8000`. MailHog web UI at `http://localhost:8025`.

See `docs/restart_espec.md` for rules on which services to restart after code changes.

### Database Migrations

```bash
# Inside the backend container or with the venv active
alembic upgrade head
alembic revision --autogenerate -m "description"
alembic downgrade -1
```

In AWS, migrations run as a one-off ECS RunTask via `run_migrations.py` (triggered by CI/CD before deploy).

### Expose Locally with Ngrok

```bash
ngrok start --config ngrok.yml --all
```

The ngrok public URL must match `NGROK_URL` in `.env` and be registered as the WhatsApp webhook in Meta's developer dashboard.

### Running Without Docker (dev only)

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
arq app.worker.WorkerSettings --watch /code
```

## Architecture

### Two-Process Architecture

The app has two processes that must both run:

1. **Backend** (`app/main.py`) — FastAPI + Uvicorn. Handles HTTP requests, WhatsApp webhooks, SSE streams, and enqueues jobs to Redis.
2. **Worker** (`app/worker.py`) — ARQ consumer. Processes WhatsApp messages asynchronously (AI intent detection, LLM calls, DB writes, SSE broadcasts). Also runs cron jobs (PIX expiry, daily cost aggregation).

### Message Flow

```
WhatsApp → POST /webhook → enqueue job to Redis (DB 1)
                                   ↓
                            ARQ Worker picks up job
                                   ↓
                     semantic_router.py (intent detection)
                                   ↓
                     prompt_central.py (LLM tool calling)
                                   ↓
                     tools execute (cart, orders, etc.)
                                   ↓
               monitoring.py records usage → Redis DB 2 + PostgreSQL
                                   ↓
                     SSE broadcast → GET /stream (dashboard)
```

### Key Directories and Files

```
app/
├── main.py              # FastAPI app, lifespan, 10 routers registered
├── worker.py            # ARQ WorkerSettings, cron jobs (PIX expiry, cost aggregation)
├── models.py            # SQLModel ORM models (16+ tables, CartState/OrderStatus/DeliveryMethod enums)
├── schemas.py           # Pydantic request/response validators (with Field constraints)
├── database.py          # Async engine, session factory
├── crud.py              # All DB CRUD operations
├── openai_client.py     # Centralized OpenAI API wrapper with per-call cost tracking
├── semantic_router.py   # Intent classification (embedding similarity, checked first)
├── prompt_central.py    # LLM prompts + tool-calling system (system → examples → history → query)
├── tools_definition.py  # Tool schemas for function calling (8 tools)
├── tool_arg_schemas.py  # Pydantic validation for LLM tool arguments
├── embedding_service.py # Lazy-loaded SentenceTransformer
├── broadcast.py         # Redis PubSub → SSE events (json.dumps, not str(dict))
├── payment_service.py   # Mercado Pago PIX creation + token refresh
├── payment_routes.py    # MP OAuth flow (with CSRF state) + payment webhooks
├── webhook_security.py  # Mercado Pago HMAC-SHA256 signature verification
├── billing_routes.py    # SaaS subscription checkout + webhooks
├── bot_routes.py        # Bot CRUD, products, orders, WhatsApp Embedded Signup
├── menu_router.py       # Menu CRUD endpoints (separated from bot_routes)
├── whatsapp.py          # Webhook handler — ~16 focused functions + MessageContext
├── auth.py              # JWT signup/login + rate-limited auth endpoints
├── rate_limiter.py      # Async Redis rate limiting (DB 0, fails closed)
├── distributed_lock.py  # Redis distributed lock for cart mutations (per contact_id)
├── encryption.py        # Fernet encryption for payment tokens at rest
├── sanitize.py          # LLM output sanitization (strips URLs, enforces length)
├── time.py              # UTC timestamp utility (replaces datetime.utcnow())
├── address_service.py   # CEP address lookup with granular timeouts
├── pending_action.py    # Pending action queue for confirmation flows
├── monitoring.py        # Usage event buffer, Redis counters, cost tracking (DB 2)
├── monitoring_routes.py # REST API: cost overview, per-bot detail, leaderboard, SSE alerts
├── health_routes.py     # /health/live (liveness) + /health/ready (PG, Redis, ARQ checks)
├── logging_config.py    # Structured JSON logging with trace correlation
├── context.py           # Request-scoped ContextVars (trace_id, bot_id, contact_id)
├── takeover_routes.py   # Human takeover activation/deactivation endpoints
├── data_extractor.py    # PDF/image menu extraction via gpt-4o
├── menu_storage.py      # S3 menu PDF/image storage operations
├── email_service.py     # Email sending via SMTP (MailHog locally)
└── clock.py             # Scheduling utilities
```

```
infra/                           # Terraform IaC (63 .tf files)
├── global/                      # ECR repo + Route 53 zone
├── environments/
│   ├── dev/                     # Dev environment wiring (LIVE)
│   └── prod/                    # Prod environment wiring (not yet provisioned)
└── modules/                     # 17 reusable modules
    ├── vpc/                     # VPC, subnets (public/private/isolated), security groups
    ├── nat/                     # Dev: fck-nat t4g.nano + Caddy reverse proxy; Prod: managed NAT Gateway
    ├── rds/                     # PostgreSQL 14 + pgvector
    ├── elasticache/             # Redis 7.0 (prod only)
    ├── redis-ecs/               # Redis 7 on ECS Fargate (dev only, replaces ElastiCache)
    ├── alb/                     # ALB + HTTPS + ACM cert (prod only; dev uses Caddy on NAT)
    ├── ecs/                     # Fargate cluster, services, task definitions
    ├── ecr/                     # Container registry
    ├── s3/                      # Menu storage bucket
    ├── secrets/                 # AWS Secrets Manager
    ├── monitoring/              # CloudWatch alarms + SNS
    ├── dashboard/               # CloudWatch dashboards
    ├── ssm-parameters/          # SSM Parameter Store (replaces Secrets Manager for dev)
    ├── scheduling/              # Off-hours ECS + Redis scaling (dev only)
    ├── rds-scheduling/          # RDS stop/start via Lambda + EventBridge (dev only)
    ├── waf/                     # AWS WAF rate limiting (prod)
    └── maintenance-page/        # S3 static failover page
```

```
docs/                            # Operational documentation
├── aws_architecture.md          # Complete AWS infrastructure reference
├── runbook_rollback.md          # Incident response + rollback procedures
├── ZenBots_observability.md     # Observability API documentation
└── restart_espec.md             # Local dev restart rules

plan/                            # Deployment planning
├── automatize_deployments_plan.md       # Master CI/CD + AWS deployment plan (dev DONE)
└── manual_steps_for_automatize_deployments.md  # Step-by-step manual provisioning guide

tech_debt/                       # Tracked backlogs (10 files)
├── backlog_architecture.md      # 24/48 resolved (50%) — architectural debt
├── backlog_mvp.md               # 7/20 resolved (35%) — conversational readiness
├── backlog_vulnerabilities.md   # 29/45 resolved (64%) — security issues
├── backlog_tests.md             # Test coverage status and gaps
├── backlog_deploy.md            # AWS deployment plan + status (dev LIVE)
├── backlog_production.md        # Production readiness assessment (45-50%)
├── backlog_token_reduction.md   # LLM cost optimization (3-phase plan, 0% started)
├── backlog_embedded_signup.md   # WhatsApp Embedded Signup notes
├── health_monitoring_backlog.md # Observability stack status (Phases 0-3 done, 4-5 partial)
└── health_monitoring_plan.md    # Step-by-step observability implementation guide
```

### Databases / External Services

| Service | Purpose | Redis DB |
|---|---|---|
| PostgreSQL 14 + pgvector | Main DB + vector search (384-dim embeddings) | — |
| Redis DB 0 | Rate limiting | 0 |
| Redis DB 1 | ARQ job queue | 1 |
| Redis DB 2 | Monitoring counters, cost tracking, anomaly detection | 2 |
| Redis PubSub | SSE broadcast (`dashboard_events`) + monitoring alerts (`monitoring_alerts`) | — |
| Ollama (phi3:mini) | Real-time LLM chat responses (local dev only) | — |
| OpenAI (gpt-4o-mini) | Real-time chat, intent classification, product extraction | — |
| OpenAI (gpt-4o) | Menu/image data extraction | — |
| Mercado Pago | PIX payments + SaaS subscriptions | — |
| AWS S3 | Menu PDF/image storage | — |
| Google Maps API | CEP address lookup | — |

### AI / LLM System

- **OpenAI client**: `openai_client.py` is the centralized wrapper for all OpenAI calls. Every call records token counts, cost, and latency to the monitoring system.
- **Intent classification**: Three-tier system. `semantic_router.py` (paraphrase-multilingual-MiniLM-L12-v2, 384-dim) runs first with ~85-90% coverage. No LLM fallback — `classify_user_intent()` eliminated (T2-3). Tiers: confident (≥ threshold) → use router intent; moderate (≥ 75% of threshold) → use router intent (except FINISH_ORDER, demoted to ADD); low (< 75%) → default to ADD. The tool-calling prompt implicitly classifies via tool selection.
- **Tool calling**: `prompt_central.py` sends multi-shot prompts (11 examples) to gpt-4o-mini via OpenAI-compatible API. Prompt order: system rules → few-shot examples → dynamic system (menu + cart + categories) → conversation history → user query. Tool arguments are validated against Pydantic schemas in `tool_arg_schemas.py`. Dynamic system message includes bot's real product categories to prevent hallucination.
- **Product search**: 4-layer search in `crud.find_relevant_products`: 1. Name ILIKE → 2. Keywords ILIKE → 3. Description ILIKE → 4. Embedding (cosine similarity with 0.7 threshold — distance < 0.3). The threshold prevents weak matches (e.g., "cachorro quente" → "Bacon"). ILIKE patterns are escaped to prevent wildcard injection.
- **LLM output sanitization**: All LLM-generated text passes through `sanitize.py` (strips URLs, emails, phone numbers, enforces length limits) before being sent to customers via WhatsApp.
- **Embedding model**: Lazy-loaded singleton in `embedding_service.py`; pre-warmed during Docker build (~400MB cached layer).
- **Cost profile**: ~3,000-4,000 input + ~150 output tokens per message (1 gpt-4o-mini call, down from 3 after Phase 1+2 optimizations). Projected ~$2,800/month at 1,000 restaurants. Token reduction plan in `tech_debt/backlog_token_reduction.md` targets further 30-50% via fine-tuning and model alternatives.
- **OpenAI operations tracked**: `get_ai_decision`, `extract_potential_items`, `get_chat_response_gpt` (gpt-4o-mini); `get_extraction_response`, `extract_products_from_image` (gpt-4o). Note: `classify_user_intent` eliminated in T2-3.

### Data Models

Core SQLModel tables: `User`, `Bot`, `Product` (with 384-dim vector), `Contact`, `ShoppingCart`, `CartItem`, `Order`, `OrderItem`, `ConversationHistory`, `PaymentConfig`, `Subscription`, `Plan`, `ProcessedMessage`, `UsageEvent`, `DailyCostSummary`.

**Enums:**
- `OrderStatus` (lowercase): `pending`, `paid`, `failed`, `expired`, `preparing`, `ready`, `completed`, `canceled`.
- `DeliveryMethod`: `delivery`, `pickup`.
- `CartState`: `GREETING` → `SHOPPING` → `AWAITING_DELIVERY_METHOD` → `AWAITING_CEP` → `AWAITING_NUMBER_COMPLEMENT` → `AWAITING_ADDRESS_CONFIRMATION` → `AWAITING_CUSTOMER_NAME` → `AWAITING_PAYMENT_METHOD`.

**Key fields:**
- `Order.webhook_token` — random `secrets.token_urlsafe(32)` for secure payment webhook URLs.
- `PaymentConfig` — stores encrypted `access_token`, `public_key`, `refresh_token` (Fernet via `app/encryption.py`).
- `ShoppingCart.human_takeover_active` — disables bot responses when human agent takes over.
- `UsageEvent` — append-only log of every external API call (tokens, cost, duration, trace_id). 90-day retention.
- `DailyCostSummary` — pre-aggregated daily costs per bot per service (cron at 3 AM UTC).

### Authentication & Security

JWT-based auth with 24h expiry. The `auth.py` router handles `/auth/signup` and `/auth/login`. Protected routes use a FastAPI dependency that decodes the JWT. Auth endpoints are rate-limited (10 req/5min on register, 5 req/5min on login/reset). Password validation requires 8+ chars, uppercase, lowercase, digit, special char, and checks against a common password blocklist.

Key security measures implemented:
- **Payment webhooks**: Mercado Pago HMAC-SHA256 signature verification (`webhook_security.py`) + webhook token per order
- **SSE stream**: Ticket-based auth (one-time use, atomic consume from Redis) + Bearer JWT + legacy token fallback
- **OAuth flow**: CSRF protection via Redis-backed `state` parameter (includes user_id + bot_id)
- **Payment tokens**: Encrypted at rest with Fernet (`ENCRYPTION_KEY` env var required)
- **MP token refresh**: `refresh_token` and `token_expires_at` tracked; auto-refresh before expiry
- **File uploads**: 10MB size limit with chunked streaming validation
- **Rate limiter**: Async Redis-based, fails closed when Redis is down
- **Cart locking**: Redis distributed lock per `contact_id` serializes cart mutations
- **LLM output**: Sanitized before sending to customers (no URLs, emails, phone numbers)
- **CORS**: Environment-aware; wildcard only in development, explicit origins required in production
- **Sensitive data**: Structured logging with PII masking; no print() calls
- **AWS WAF** (prod): Rate limiting 2,000 req/5min per IP

### Observability & Monitoring

The observability stack is implemented in Phases 0-3 (Phases 4-5 partially done). Full documentation in `docs/ZenBots_observability.md`.

**Health endpoints** (no auth, used by ALB):
- `GET /health/live` — liveness probe, returns `{"status": "alive"}`
- `GET /health/ready` — readiness probe, checks PostgreSQL + Redis DB 0 + Redis DB 1 (ARQ queue depth)

**Monitoring endpoints** (JWT required, ownership-enforced):
- `GET /monitoring/overview?days=N` — aggregate cost across all user's bots
- `GET /monitoring/bot/{bot_id}?days=N` — per-bot daily cost with real-time today cost from Redis
- `GET /monitoring/leaderboard?days=N&limit=N` — ranks bots by cost
- `GET /monitoring/alerts/stream` — SSE stream for real-time anomaly alerts

**Structured logging**: JSON format in production (`python-json-logger`), text in dev. Fields: `asctime`, `levelname`, `name`, `message`, `trace_id`, `bot_id`, `contact_id`. Configured via `LOG_FORMAT=text` env var for local dev.

**Tracing**: Every WhatsApp message gets a 12-char hex `trace_id` generated in `process_whatsapp_message`, injected into all logs and `usage_events.trace_id` for end-to-end correlation.

**Cost tracking**: Every external API call is instrumented via `monitoring.py`. In-process event buffer flushes every 50 events or 10 seconds (hard cap 500) to PostgreSQL + Redis DB 2. Services tracked: `openai`, `google_maps`, `aws_s3`, `whatsapp`, `mercado_pago`, `facebook`.

**Anomaly detection**: Compares real-time hourly cost vs 7-day average. Warning at >3x, critical at >10x. Deduped to one alert per bot per hour.

**CloudWatch** (AWS): 13 alarms (ECS CPU/memory, ALB 5xx/latency, RDS CPU/storage/connections, Redis CPU/memory/evictions). All alerts → SNS → email.

### Real-Time Dashboard (SSE)

`GET /stream` is a Server-Sent Events endpoint. The worker publishes events via `broadcast.py` → Redis PubSub channel `dashboard_events:{bot_id}`. The frontend consumes events via the native `EventSource` API (`withCredentials: true` for cookie auth). SSE responses include `X-Accel-Buffering: no` for Nginx compatibility.

**Key implementation details:**
- Backend sends data-level pings every 10s (`data: {"type":"ping"}`) to keep connections alive through proxies. SSE comments (`: keep-alive`) are not used — proxies may not recognize them as activity.
- Caddy reverse proxy (dev) requires `flush_interval -1` and `transport http { read_timeout 0; write_timeout 0 }` for SSE streaming.
- **Critical**: The DB transaction must be committed BEFORE broadcasting via Redis PubSub. Otherwise the frontend refetches orders before the new order is visible to other DB sessions (race condition). ORM scalar values must be captured into local variables before commit (which expires all objects).
- Frontend uses `EventSource` (not `fetch` + `ReadableStream`) — the latter fails silently through HTTP/2 due to frame-level buffering.
- Frontend has a 5s polling fallback (`refetchInterval`) when SSE is disconnected.

### Payment Flow

1. **OAuth**: User connects Mercado Pago account via `/payments/auth-url` → callback at `/payments/callback` stores encrypted credentials in `PaymentConfig`. OAuth state validated via Redis to prevent CSRF. Bot ID preserved through the flow.
2. **PIX**: Order finalization calls `payment_service.py` to create a MP charge → QR code sent to customer via WhatsApp. Access tokens auto-refreshed when nearing expiry.
3. **Webhook**: `POST /payments/webhooks/payment-confirm/{order_id}?token=...` validates HMAC signature + per-order webhook token before updating order status and broadcasting via SSE.
4. **PIX expiry**: ARQ cron job runs every 5 minutes to cancel PIX orders pending >15 minutes.
5. **Subscriptions**: `POST /billing/checkout` creates recurring MP subscription; access gated by `current_period_end`.

### WhatsApp Embedded Signup

Restaurant owners connect their WhatsApp Business number through Facebook's Embedded Signup flow:
1. Frontend loads FB JS SDK, opens login popup with Embedded Signup config
2. Happy path: `WA_EMBEDDED_SIGNUP` postMessage provides phone_number_id, waba_id directly
3. Fallback (5s timeout): Uses access token from FB.login callback; backend discovers IDs via Graph API
4. Backend exchanges short-lived token for long-lived token, saves credentials to Bot record
5. Currently in **dev mode** — requires Facebook App Review for production (`whatsapp_business_management` + `whatsapp_business_messaging` permissions)

## AWS Deployment

### Current State

- **Dev environment**: **LIVE** at `https://dev-api.zenbotz.com.br`. 95 Terraform resources provisioned.
- **Prod environment**: Terraform config exists but **not yet provisioned** (planned Month 2).
- **AWS account**: `578761488332`, region: `us-east-1`.
- **Domain**: `zenbotz.com.br` (Route 53 zone `Z0672127E335XW159Q8N`, NS migrated from HostGator).

### Infrastructure

| Component | Dev | Prod (planned) |
|---|---|---|
| **ECS Fargate** | On-demand FARGATE ARM64 (`base=1` pins — Spot reclaims broke availability in June 2026), backend 0.5 vCPU / 2 GB (1 task), worker 0.5 vCPU / 1 GB (1 task) | FARGATE ARM64, backend 1 vCPU / 2 GB 2-4 tasks (auto-scale), worker 0.25 vCPU / 512 MB 1-2 tasks |
| **RDS PostgreSQL 14** | db.t4g.micro, single-AZ, 20 GB, 7-day backups, scheduled ~50 hrs/wk | db.t4g.small, Multi-AZ, 20 GB→100 GB, 14-day backups, deletion protection |
| **Redis** | Redis 7 on ECS Fargate on-demand (0.25 vCPU / 512 MB), Cloud Map DNS | ElastiCache cache.t4g.small, 2 nodes Multi-AZ |
| **NAT** | fck-nat t4g.nano + Caddy reverse proxy (~$3/mo) | Managed NAT Gateway (~$32/mo) |
| **HTTPS** | Caddy on NAT instance (Let's Encrypt TLS, Cloud Map → ECS) | ALB + ACM wildcard cert + WAF (2,000 req/5min) |
| **Scheduling** | 9 AM-7 PM BRT weekdays only (~50 hrs/wk). ECS + RDS + Redis all scheduled. | Always on |
| **Est. cost** | ~$27/month pre-tax (~$31 invoiced) | ~$108/month (launch), ~$200/month (scaled) |

**VPC**: `10.0.0.0/16` with 3 tiers across 2 AZs: public (NAT/ALB), private (ECS), isolated (RDS/Redis). S3 Gateway Endpoint (free, S3 traffic bypasses NAT).

**ECR**: Single repo `zenbots/app` at `578761488332.dkr.ecr.us-east-1.amazonaws.com/zenbots/app`. Tags: `dev-{SHA8}`, `prod-{SHA8}`, `latest-dev`. Lifecycle: untagged deleted after 7 days, keep last 10 tagged.

**Secrets**: Dev uses SSM Parameter Store (free tier, 19 params). Old Secrets Manager entries (8 secrets) still exist pending cleanup. Prod will also use SSM. Injected into ECS tasks at launch.

**Terraform state**: S3 bucket `zenbots-terraform-state` + DynamoDB lock table `zenbots-terraform-locks`.

### CI/CD Pipelines

Four GitHub Actions workflows in `.github/workflows/`:

1. **`pr-checks.yml`** — On PRs: Ruff lint + format, pytest, Docker build validation.
2. **`deploy-dev.yml`** — On push to `develop`: lint → test → build ARM64 image → ECR push → ECS RunTask migration → deploy backend + worker → 8-min stabilization wait → smoke test (`/health/ready`). Uses OIDC auth, cancel-in-progress.
3. **`deploy-prod.yml`** — On push to `main`: same pipeline + manual approval gate + pre-migration RDS snapshot + zero-downtime deploy (min healthy 100%).
4. **`terraform.yml`** — On `infra/**` changes: plan posted as PR comment, apply requires manual approval.

All workflows use AWS OIDC federation (no long-lived keys). Three IAM roles: `github-actions-dev`, `github-actions-prod`, `github-actions-terraform`.

**Docker build**: Multi-stage (base → backend/worker/migrations), ARM64 (Graviton), Python 3.10, non-root user, embedding model pre-warmed. `PYTHONDONTWRITEBYTECODE=1` set in Dockerfile and `docker-compose.yml` to prevent stale `.pyc` bytecode cache issues (critical for volume-mounted code on Windows/Docker and for clean deploys).

### Rollback

See `docs/runbook_rollback.md` for full procedures. Key scenarios:
- **Migration fails**: Pipeline aborts, old containers keep running (zero downtime).
- **Code fails health checks**: ECS circuit breaker auto-rolls back (~2 min).
- **Runtime bugs**: Manual ECS rollback to previous task definition revision.
- **DB corruption**: RDS snapshot restore (prod creates `pre-deploy-*` snapshots before every migration).
- **ALB unreachable**: Route 53 health check failover to S3 maintenance page (auto-recovers).

## Environment Variables

Loaded from `.env` locally, from AWS Secrets Manager in AWS. Key variables:
- `OPENAI_API_KEY`, `OPENAI_BASE_URL` (points to Ollama locally, OpenAI API in AWS)
- `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`, `VERIFY_TOKEN`
- `META_APP_ID`, `META_APP_SECRET`
- `FB_APP_ID`, `FB_APP_SECRET` (Embedded Signup)
- `MP_CLIENT_ID`, `MP_CLIENT_SECRET`, `MP_REDIRECT_URI`, `MP_WEBHOOK_SECRET`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME`, `S3_REGION`
- `DATABASE_URL` (asyncpg format)
- `SECRET_KEY` (JWT signing — validated on startup, raises RuntimeError if missing)
- `ENCRYPTION_KEY` (Fernet key for encrypting payment tokens at rest)
- `GOOGLE_MAPS_API_KEY`
- `CORS_ORIGINS` or `CORS_ORIGIN_REGEX` (production CORS — wildcard only in dev)
- `ENVIRONMENT` (controls CORS strictness, logging level, log format)
- `LOG_FORMAT` (`text` for local dev, JSON in production)

## Key Architecture Decisions

1. **whatsapp.py refactoring**: The original ~1,000-line `process_whatsapp_message` God Function was split into ~16 focused functions with a `MessageContext` dataclass. All functions kept in the same file to preserve test mock paths. Gate/guard functions, checkout state handlers, intent handlers, and shopping logic are clearly separated.

2. **Cart state machine**: `CartState` enum replaced string literals. States flow: `GREETING` → `SHOPPING` → `AWAITING_DELIVERY_METHOD` → `AWAITING_CEP` → `AWAITING_NUMBER_COMPLEMENT` → `AWAITING_ADDRESS_CONFIRMATION` → `AWAITING_CUSTOMER_NAME` → `AWAITING_PAYMENT_METHOD`.

3. **Multi-tool dispatch**: The tool dispatch loop processes `ai_message.tool_calls` (not just `[0]`). Cart-modifying tools break after execution; conversational tools continue processing additional tool calls.

4. **Error classification**: `_classify_error_message(exc)` returns specific Portuguese error messages for timeout/connection, database, LLM, and payment errors instead of one generic string.

5. **Timezone handling**: All timestamps use `datetime.now(timezone.utc)` via the `utcnow()` helper in `app/time.py`. No `datetime.utcnow()` calls remain.

6. **Centralized OpenAI client**: All OpenAI API calls go through `openai_client.py`, which wraps each call with cost tracking (token counts, pricing, latency) and writes usage events to the monitoring system.

7. **Monitoring buffer pattern**: Usage events are buffered in-process (flush at 50 events or 10s, hard cap 500) to avoid per-call DB writes. Redis DB 2 provides real-time counters; PostgreSQL provides durable history.

8. **ARM64 Graviton**: Docker images are built for `linux/arm64` to run on Fargate Graviton (20% cheaper than x86). Cross-compiled via `docker/setup-qemu-action` in CI.

9. **Dev cost optimization**: ECS services (backend, worker, Redis) scale to 0 between 7 PM-9 AM BRT on weekdays and all weekend (~50 hrs/week running). RDS stops/starts on the same schedule. ElastiCache replaced by Redis on ECS. ALB replaced by Caddy on fck-nat instance. Secrets Manager deleted (using SSM). All three ECS services pinned to on-demand FARGATE (`base=1`) after June 2026 Spot reclaims broke availability. Total dev cost: ~$27/month pre-tax (~$31 invoiced with BR tax). Guards: AWS Budget `zenbots-monthly-30` emails at 80%/100%, and `terraform.yml` has a post-apply check for the 9 ECS scheduled scaling actions (replacing an ECS service silently deletes its autoscaling target + schedule — this left Redis running 24/7 for most of June 2026).

10. **Async SQLAlchemy relationship loading**: Never use `session.refresh(obj, attribute_names=["relationship"])` in async context — it expires ALL attributes on the object, causing `greenlet_spawn` errors on subsequent access. Instead, use direct `SELECT` queries + `set_committed_value` from `sqlalchemy.orm.attributes` (see `_load_cart_items_with_products()` and `_load_bot_payment_config()` in `whatsapp.py`). Similarly, `session.commit()` and `session.rollback()` expire all objects — capture scalar values into local variables before any code path that might commit/rollback.

11. **Broadcast after commit**: SSE broadcasts (`broadcast_order_update`) must happen AFTER `session.commit()`, not before. The frontend refetches data immediately upon receiving the SSE event — if the transaction isn't committed yet, the refetch returns stale data (order not visible to other DB sessions).

12. **Suggestion selection handler**: `_handle_suggestion_selection` in `whatsapp.py` runs before intent classification for messages with active `last_suggestions`. Supports multi-selection ("dois do primeiro e tres do segundo"), ordinal/digit/name-based matching, quantity parsing (written + digit), Portuguese plural stemming ("paranaenses" → "paranaense"), and deduplication. Splits on "e", ",", ";" and processes each part independently. Falls through to normal flow on no match (suggestions preserved for CONFIRM handler).

13. **Programmatic em falta**: Unavailable product messages are built programmatically (not by the LLM). `unavailable_products=[]` is passed to the LLM prompt so it focuses on adding valid items. The em falta text is appended to `response_to_user` after the dispatch loop. This prevents the LLM from fixating on unavailability and ignoring valid items in multi-item orders.

14. **Option C (conversational override)**: When the LLM uses `answer_conversationally` for a shopping intent (ADD, MODIFY, REMOVE, REQUEST_SUGGESTION) without adding anything to the cart, the response is replaced with a numbered product list from `_get_meal_suggestions`. This eliminates hallucinated menu categories and reduces conversation turns. Guarded by `not unavailable_matches` to preserve the em falta flow.

15. **Meal suggestion filtering**: `_filter_meal_suggestions` excludes addon/drink categories (Adicionais, Bebidas, Cervejas, Extras, Complementos, Acompanhamentos) and sorts by price descending. `_get_meal_suggestions` enforces min 3 / max 4 results with a DB fallback query when filtering removes all products.
