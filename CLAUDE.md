# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ZenBots** is a SaaS platform that lets restaurant owners deploy AI-powered WhatsApp chatbots for automated order management (cart, payments, delivery). It uses a Python FastAPI backend with a local LLM (Ollama/phi3:mini) for real-time chat and OpenAI (gpt-4o) for menu extraction.

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
├── worker.py            # ARQ WorkerSettings, job function registration
├── models.py            # All SQLModel ORM models (14 tables)
├── schemas.py           # Pydantic request/response validators
├── database.py          # Async engine, session factory
├── crud.py              # All DB CRUD operations
├── semantic_router.py   # Intent classification (embedding similarity)
├── prompt_central.py    # LLM prompts + tool-calling system
├── tools_definition.py  # Tool schemas for function calling
├── embedding_service.py # Lazy-loaded SentenceTransformer
├── broadcast.py         # Redis PubSub → SSE events
├── payment_service.py   # Mercado Pago PIX payment creation
├── payment_routes.py    # MP OAuth flow + payment webhooks
├── billing_routes.py    # SaaS subscription checkout + webhooks
├── bot_routes.py        # Bot CRUD, products, orders
├── whatsapp.py          # Webhook handler + SSE endpoint
├── auth.py              # JWT signup/login
└── rate_limiter.py      # Redis DB 0 spam prevention
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

- **Intent classification**: `semantic_router.py` uses `paraphrase-multilingual-MiniLM-L12-v2` (384-dim embeddings) to classify user messages into intents (SHOW_CART, ADD, FINISH_ORDER, etc.) before calling the LLM.
- **Tool calling**: `prompt_central.py` sends multi-shot prompts to Ollama expecting JSON responses with `tool_name` and `args`.
- **Product search**: Semantic search via pgvector cosine similarity on product embeddings.
- **Embedding model**: Lazy-loaded singleton in `embedding_service.py`; pre-warmed during Docker build.

### Data Models

Core SQLModel tables: `User`, `Bot`, `Product` (with 384-dim vector), `Contact`, `ShoppingCart`, `CartItem`, `Order`, `OrderItem`, `ConversationHistory`, `PaymentConfig`, `Subscription`, `Plan`, `ProcessedMessage`.

`OrderStatus` enum values are lowercase: `pending`, `paid`, `failed`, `expired`, `preparing`, `ready`, `completed`, `canceled`.
`DeliveryMethod` enum: `DELIVERY`, `PICKUP`.

### Authentication

JWT-based auth with 24h expiry. The `auth.py` router handles `/auth/signup` and `/auth/login`. Protected routes use a FastAPI dependency that decodes the JWT.

### Real-Time Dashboard (SSE)

`GET /stream` is a Server-Sent Events endpoint. The worker publishes events via `broadcast.py` → Redis PubSub channel `dashboard_events`. The frontend polls this endpoint for live order/payment updates. SSE responses include `X-Accel-Buffering: no` for Nginx compatibility.

### Payment Flow

1. **OAuth**: User connects Mercado Pago account via `/payments/auth-url` → callback at `/payments/callback` stores credentials in `PaymentConfig`.
2. **PIX**: Order finalization calls `payment_service.py` to create a MP charge → QR code sent to customer via WhatsApp.
3. **Webhook**: `POST /payments/webhooks/payment-confirm/{order_id}` updates order status and broadcasts via SSE.
4. **Subscriptions**: `POST /billing/checkout` creates recurring MP subscription; access gated by `current_period_end`.

## Environment Variables

Loaded from `.env`. Key variables:
- `OPENAI_API_KEY`, `OPENAI_BASE_URL` (points to Ollama locally)
- `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`, `VERIFY_TOKEN`
- `META_APP_ID`, `META_APP_SECRET`
- `MP_CLIENT_ID`, `MP_CLIENT_SECRET`, `MP_REDIRECT_URI`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME`
- `DATABASE_URL` (asyncpg format)
- `SECRET_KEY` (JWT signing)
- `GOOGLE_MAPS_API_KEY`
