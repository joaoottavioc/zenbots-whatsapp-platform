# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ZenBots** is a SaaS platform that lets restaurant owners deploy AI-powered WhatsApp chatbots for automated order management (cart, payments, delivery). It uses a Python FastAPI backend with OpenAI (gpt-4o-mini) for real-time tool-calling chat and OpenAI (gpt-4o) for menu extraction. Local development uses Ollama (phi3:mini) as an optional LLM backend via the `OPENAI_BASE_URL` env var.

### Current Maturity

The platform is in **active development** heading toward production deployment. Key stats:
- **Test suite**: 182 tests, 52% codebase coverage (175 pass, 7 fail due to bcrypt/Python 3.14 compat)
- **Tech debt**: Tracked across 8 backlog files in `tech_debt/`. Major refactorings complete (God Function split, enum-based state machine, security hardening). See `tech_debt/` for full status.
- **WhatsApp Embedded Signup**: Working in dev mode (Facebook app review required for production)
- **Deployment**: AWS deployment plan finalized (ECS Fargate + RDS + ElastiCache + Terraform). Not yet implemented.

## Running the Project

All services run via Docker Compose:

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

### Database Migrations

```bash
# Inside the backend container or with the venv active
alembic upgrade head
alembic revision --autogenerate -m "description"
alembic downgrade -1
```

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
2. **Worker** (`app/worker.py`) — ARQ consumer. Processes WhatsApp messages asynchronously (AI intent detection, LLM calls, DB writes, SSE broadcasts).

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
                     SSE broadcast → GET /stream (dashboard)
```

### Key Directories and Files

```
app/
├── main.py              # FastAPI app, lifespan, router registration
├── worker.py            # ARQ WorkerSettings, cron jobs (PIX expiry), job registration
├── models.py            # SQLModel ORM models (14+ tables, CartState enum, OrderStatus enum)
├── schemas.py           # Pydantic request/response validators (with Field constraints)
├── database.py          # Async engine, session factory
├── crud.py              # All DB CRUD operations
├── semantic_router.py   # Intent classification (embedding similarity, checked first)
├── prompt_central.py    # LLM prompts + tool-calling system (reordered: system → examples → history → query)
├── tools_definition.py  # Tool schemas for function calling (8 tools, 3 phantom tools removed)
├── tool_arg_schemas.py  # Pydantic validation for LLM tool arguments
├── embedding_service.py # Lazy-loaded SentenceTransformer
├── broadcast.py         # Redis PubSub → SSE events (json.dumps, not str(dict))
├── payment_service.py   # Mercado Pago PIX creation + token refresh
├── payment_routes.py    # MP OAuth flow (with CSRF state) + payment webhooks (signature verified)
├── billing_routes.py    # SaaS subscription checkout + webhooks
├── bot_routes.py        # Bot CRUD, products, orders, WhatsApp Embedded Signup onboarding
├── whatsapp.py          # Webhook handler — refactored into ~16 focused functions + MessageContext
├── auth.py              # JWT signup/login + rate-limited auth endpoints
├── rate_limiter.py      # Async Redis rate limiting (DB 0, fails closed)
├── distributed_lock.py  # Redis distributed lock for cart mutations (per contact_id)
├── encryption.py        # Fernet encryption for payment tokens at rest
├── sanitize.py          # LLM output sanitization (strips URLs, enforces length)
├── time.py              # UTC timestamp utility (replaces datetime.utcnow())
├── address_service.py   # CEP address lookup with granular timeouts
└── pending_action.py    # Pending action queue for confirmation flows
```

```
tech_debt/                    # Tracked backlogs (8 files)
├── backlog_architecture.md   # 24/48 resolved (50%) — architectural debt
├── backlog_mvp.md            # 7/20 resolved (35%) — conversational readiness
├── backlog_vulnerabilities.md # 29/45 resolved (64%) — security issues
├── backlog_tests.md          # Test coverage status and gaps
├── backlog_deploy.md         # AWS deployment plan (ECS, Terraform, CI/CD)
├── backlog_token_reduction.md # LLM cost optimization (3-phase plan)
├── backlog_embedded_signup.md # WhatsApp Embedded Signup notes
└── health_monitoring_backlog.md # Per-bot cost attribution + anomaly detection plan
```

### Databases / External Services

| Service | Purpose | Redis DB |
|---|---|---|
| PostgreSQL + pgvector | Main DB + vector search | — |
| Redis DB 0 | Rate limiting | 0 |
| Redis DB 1 | ARQ job queue | 1 |
| Redis PubSub | SSE broadcast channel (`dashboard_events`) | — |
| Ollama (phi3:mini) | Real-time LLM chat responses | — |
| OpenAI (gpt-4o) | Menu/image data extraction | — |
| Mercado Pago | PIX payments + subscriptions | — |
| AWS S3 | Menu PDF/image storage | — |

### AI / LLM System

- **Intent classification**: Two-tier system. `semantic_router.py` (paraphrase-multilingual-MiniLM-L12-v2, 384-dim) runs first and handles ~60-70% of messages. LLM fallback (`classify_user_intent`) only fires when router confidence is below a dynamic threshold.
- **Tool calling**: `prompt_central.py` sends multi-shot prompts (13 examples) to gpt-4o-mini via OpenAI-compatible API. Prompt order: system rules → few-shot examples → conversation history → user query. Tool arguments are validated against Pydantic schemas in `tool_arg_schemas.py`.
- **Product search**: Semantic search via pgvector cosine similarity on product embeddings. ILIKE patterns are escaped to prevent wildcard injection.
- **LLM output sanitization**: All LLM-generated text passes through `sanitize.py` (strips URLs, emails, phone numbers, enforces length limits) before being sent to customers via WhatsApp.
- **Embedding model**: Lazy-loaded singleton in `embedding_service.py`; pre-warmed during Docker build.
- **Cost profile**: ~6,500 input + ~150 output tokens per message across up to 3 gpt-4o-mini calls. Projected ~$9,600/month at 1,000 restaurants. Token reduction plan in `tech_debt/backlog_token_reduction.md` targets 60-85% reduction.

### Data Models

Core SQLModel tables: `User`, `Bot`, `Product` (with 384-dim vector), `Contact`, `ShoppingCart`, `CartItem`, `Order`, `OrderItem`, `ConversationHistory`, `PaymentConfig`, `Subscription`, `Plan`, `ProcessedMessage`.

`OrderStatus` enum values are lowercase: `pending`, `paid`, `failed`, `expired`, `preparing`, `ready`, `completed`, `canceled`.
`DeliveryMethod` enum: `DELIVERY`, `PICKUP`.
`CartState` enum: `GREETING`, `SHOPPING`, `AWAITING_DELIVERY_METHOD`, `AWAITING_CEP`, `AWAITING_NUMBER_COMPLEMENT`, `AWAITING_ADDRESS_CONFIRMATION`, `AWAITING_CUSTOMER_NAME`, `AWAITING_PAYMENT_METHOD`. Replaced free-form string literals throughout the codebase.

Orders have a `webhook_token` field (random `secrets.token_urlsafe(32)`) for secure payment webhook URLs. PaymentConfig stores encrypted `access_token`, `public_key`, and `refresh_token` (Fernet encryption via `app/encryption.py`).

### Authentication & Security

JWT-based auth with 24h expiry. The `auth.py` router handles `/auth/signup` and `/auth/login`. Protected routes use a FastAPI dependency that decodes the JWT. Auth endpoints are rate-limited (10 req/5min on register, 5 req/5min on login/reset). Password validation requires 8+ chars, uppercase, lowercase, digit, special char, and checks against a common password blocklist.

Key security measures implemented:
- **Payment webhooks**: Mercado Pago HMAC-SHA256 signature verification + webhook token per order
- **SSE stream**: Requires JWT auth, events filtered to authenticated user's bots
- **OAuth flow**: CSRF protection via Redis-backed `state` parameter (includes user_id + bot_id)
- **Payment tokens**: Encrypted at rest with Fernet (`ENCRYPTION_KEY` env var required)
- **MP token refresh**: `refresh_token` and `token_expires_at` tracked; auto-refresh before expiry
- **File uploads**: 10MB size limit with chunked streaming validation
- **Rate limiter**: Async Redis-based, fails closed when Redis is down
- **Cart locking**: Redis distributed lock per `contact_id` serializes cart mutations
- **LLM output**: Sanitized before sending to customers (no URLs, emails, phone numbers)
- **CORS**: Environment-aware; wildcard only in development, explicit origins required in production
- **Sensitive data**: `print()` calls replaced with `logging` module; PII masked in logs

### Real-Time Dashboard (SSE)

`GET /stream` is a Server-Sent Events endpoint. The worker publishes events via `broadcast.py` → Redis PubSub channel `dashboard_events`. The frontend polls this endpoint for live order/payment updates. SSE responses include `X-Accel-Buffering: no` for Nginx compatibility.

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

### Deployment Strategy (Planned)

Target: AWS with dev and production environments. Full plan in `tech_debt/backlog_deploy.md`.
- **Compute**: ECS Fargate (backend + worker as separate services)
- **Database**: RDS PostgreSQL with pgvector (Multi-AZ in prod)
- **Cache**: ElastiCache Redis (2 nodes in prod)
- **LLM**: OpenAI API only in AWS (no Ollama — saves ~$70-150/month). `OPENAI_BASE_URL` points to Ollama locally, to OpenAI's API in AWS. No code changes needed.
- **IaC**: Terraform with S3 remote state, modular structure in `infra/`
- **CI/CD**: GitHub Actions — push to `develop` auto-deploys to dev, push to `main` deploys to prod with manual approval
- **Secrets**: AWS Secrets Manager (no `.env` files in AWS)
- **Estimated cost**: ~$100/month (dev), ~$275/month (prod initial)

## Environment Variables

Loaded from `.env`. Key variables:
- `OPENAI_API_KEY`, `OPENAI_BASE_URL` (points to Ollama locally, OpenAI API in production)
- `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`, `VERIFY_TOKEN`
- `META_APP_ID`, `META_APP_SECRET`
- `FB_APP_ID`, `FB_APP_SECRET` (Embedded Signup)
- `MP_CLIENT_ID`, `MP_CLIENT_SECRET`, `MP_REDIRECT_URI`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME`
- `DATABASE_URL` (asyncpg format)
- `SECRET_KEY` (JWT signing — validated on startup, raises RuntimeError if missing)
- `ENCRYPTION_KEY` (Fernet key for encrypting payment tokens at rest)
- `GOOGLE_MAPS_API_KEY`
- `CORS_ORIGINS` or `CORS_ORIGIN_REGEX` (production CORS — wildcard only in dev)
- `ENVIRONMENT` (controls CORS strictness, logging level)

## Key Architecture Decisions

1. **whatsapp.py refactoring**: The original ~1,000-line `process_whatsapp_message` God Function was split into ~16 focused functions with a `MessageContext` dataclass. All functions kept in the same file to preserve test mock paths. Gate/guard functions, checkout state handlers, intent handlers, and shopping logic are clearly separated.

2. **Cart state machine**: `CartState` enum replaced string literals. States flow: `GREETING` → `SHOPPING` → `AWAITING_DELIVERY_METHOD` → `AWAITING_CEP` → `AWAITING_NUMBER_COMPLEMENT` → `AWAITING_ADDRESS_CONFIRMATION` → `AWAITING_CUSTOMER_NAME` → `AWAITING_PAYMENT_METHOD`.

3. **Multi-tool dispatch**: The tool dispatch loop processes `ai_message.tool_calls` (not just `[0]`). Cart-modifying tools break after execution; conversational tools continue processing additional tool calls.

4. **Error classification**: `_classify_error_message(exc)` returns specific Portuguese error messages for timeout/connection, database, LLM, and payment errors instead of one generic string.

5. **Timezone handling**: All timestamps use `datetime.now(timezone.utc)` via the `utcnow()` helper in `app/time.py`. No `datetime.utcnow()` calls remain.
