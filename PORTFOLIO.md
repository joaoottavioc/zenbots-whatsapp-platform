# Portfolio Notes — AI Engineering Case Study

A recruiter-friendly walkthrough of the AI engineering decisions in this
codebase, with file-and-line references. Less than 5 minutes to read.

If you're screening this repo for an AI engineer role: this is what to
look at, what tradeoffs I considered, and the production realities that
shaped each call.

---

## Who & what

**João Ribeiro** — building AI systems in production, looking for AI
engineer roles. `joaoottavioc@gmail.com`.

**ZenBotZ** — a multi-tenant chatbot platform for restaurants. Customer
chats in Portuguese, bot adds items to a cart, asks for address, generates
PIX payment, broadcasts orders to a kitchen-display dashboard in real
time. Three channels share one core: web widget (live), WhatsApp
(deferred pending Meta App Review), future Instagram/Telegram.

The bot is **not** a thin LLM wrapper. The LLM is one stage in a longer
pipeline that does meaningful work *before* and *after* the model call.

---

## What this code shows that most "AI engineer" repos don't

### 1. Custom semantic router → 67% reduction in LLM calls

**File:** [`app/semantic_router.py`](app/semantic_router.py)

A 3-tier intent classifier sits in front of every message:

1. Encode the user message with `paraphrase-multilingual-MiniLM-L12-v2`
   (384-dim, lazy-loaded singleton).
2. Cosine-compare against per-intent example pools (`ADD`, `REMOVE`,
   `MODIFY`, `CONFIRM`, `REQUEST_SUGGESTION`, `FINISH_ORDER`, …).
3. Apply tiered thresholds:
   - **confident** (≥ τ): use router intent, skip the LLM intent call.
   - **moderate** (≥ 0.75·τ): use router intent (with FINISH_ORDER demoted to ADD as a guardrail).
   - **low** (< 0.75·τ): default to ADD; let the tool-calling LLM disambiguate.

**Result:** the previous `classify_user_intent` LLM call was eliminated
entirely (commit log: T2-3 elimination). For a 1k-restaurant scale, that
removes one full gpt-4o-mini call per message — ~$280/month savings before
any other optimization.

**Talking point:** "When do you use an LLM vs. an embedding?" — Answer
in the code.

### 2. 4-layer retrieval over restaurant menus (RAG done right)

**File:** [`app/crud.py`](app/crud.py) — `find_relevant_products`

Most "RAG" implementations do `embeddings.search(query)` and call it a
day. This one is explicit about *when* vector search helps and when it
hurts:

```python
# Layer 1: ILIKE on product name (escaped against wildcard injection)
# Layer 2: ILIKE on the bot's curated keywords
# Layer 3: ILIKE on description
# Layer 4: pgvector cosine — threshold 0.7 (distance < 0.3)
```

**Why this order:** restaurant menus are short, owner-curated lists of
~50-200 items. The names are the canonical retrieval key. Embeddings
catch the long-tail ("cachorro quente" → "Hot Dog") but produce
hallucinated matches if you ask them first ("cachorro quente" → "Bacon"
at distance 0.5). The 0.3-distance threshold was tuned via the QA corpus.

**Talking point:** "Why didn't you just use a vector DB?" — Because
the answer to most product lookups is `LIKE 'pizza%'`.

### 3. Tool calling with Pydantic boundary defense

**Files:** [`app/tool_arg_schemas.py`](app/tool_arg_schemas.py),
[`app/prompt_central.py`](app/prompt_central.py),
[`app/tools_definition.py`](app/tools_definition.py)

The LLM is exposed to 8 tools (`add_items_to_cart`, `update_quantity`,
`remove_items_from_cart`, `request_suggestions`, `answer_conversationally`,
…). Every tool call's arguments pass through a Pydantic schema *before*
hitting any DB write or cart mutation.

This is the security-of-AI-systems version of input validation. The LLM
*will* occasionally return malformed JSON, hallucinate field names, or
emit nonsense values. Pydantic stops it at the boundary.

**Talking point:** "How do you keep the LLM from corrupting state?" —
Answer: it can't touch state directly. It proposes tool calls; validation
+ the handler enforce invariants.

### 4. Per-call cost tracking + production anomaly detection

**File:** [`app/monitoring.py`](app/monitoring.py)

Every call to OpenAI, Groq, Mercado Pago, Google Maps, WhatsApp Cloud
API, S3, or Facebook writes a `UsageEvent` row:

```
operation, model, tokens_prompt, tokens_completion,
cost_usd, duration_ms, success, trace_id, bot_id, channel
```

In-process buffer (flushes every 50 events or 10s, hard cap 500) avoids
per-call DB writes. Redis DB 2 holds real-time hourly counters per bot.

A nightly cron computes a 7-day rolling average. Real-time hourly cost
> 3× → warning alert; > 10× → critical alert. Deduped to one alert per
bot per hour. Alerts publish via Redis PubSub to a dashboard SSE stream
the owner can subscribe to.

**Talking point:** "How do you know if a bot is going off the rails
cost-wise?" — Live dashboard tells you within the hour.

### 5. Channel isolation contract

**Files:** [`plan/in_browser_bots.md`](plan/in_browser_bots.md),
`app/whatsapp.py:MessageContext`, `app/web_channel.py`

When the web widget was added, the temptation was to copy-paste the
WhatsApp handler. Instead the system was refactored around three seams:

- **Ingress** — WhatsApp webhook vs. `POST /chat/{bot_id}/message`.
  Each owns its own auth, rate-limit, dedup.
- **Identity synthesis** — WhatsApp uses real E.164; web synthesizes
  `Contact.phone_number = "web:{session_id}"`. The `Contact` table now
  has a `channel` column to dispatch correctly.
- **Egress** — `MessageContext.reply()` dispatches by `ctx.channel`.
  WhatsApp → `send_whatsapp_message` (Meta Graph API). Web →
  `broadcast_web_reply` (Redis PubSub → SSE).

Everything between ingress and egress — semantic routing, LLM calls,
tool dispatch, DB writes, cart state machine, payment flow — is shared.

Adding a third channel (Instagram DM, Telegram) is roughly a one-file
ingress endpoint and one-line egress dispatch entry. The pipeline doesn't
care.

**Talking point:** "How do you handle multi-channel AI without duplicating
logic?" — Show this contract.

### 6. Voice with provider redundancy

**File:** [`app/openai_client.py`](app/openai_client.py) — `transcribe_audio`

Customer voice notes (WhatsApp audio messages, web widget voice input)
go through Whisper. Primary: Groq's `whisper-large-v3-turbo` (~10× cheaper,
faster). On any failure, automatic fallback to OpenAI Whisper-1.

Conditioning prompt is built dynamically from the bot's product catalog
(`_build_whisper_prompt`) — boosts accuracy on Portuguese restaurant
terms ("paranaense", "calabresa", "esfiha") that generic Whisper
mis-transcribes.

**Talking point:** "What happens when your LLM provider is down?" —
Build with fallbacks from day one.

### 7. Eval harness — not just unit tests

**Files:** [`tests/simulation/corpus/`](tests/simulation/corpus/),
[`tests/simulation/corpus/run_corpus_qa.py`](tests/simulation/corpus/run_corpus_qa.py) (1419 LOC)

Two layers:

- **Unit tests** (~442) — handlers, CRUD, route contracts, schemas.
  Run on every PR.
- **QA corpus** — ground-truth conversations with expected outcomes.
  Each scenario walks a simulated customer through a bot interaction
  and scores whether the bot understood the request. Current baseline:
  ~79.4% comprehension. Regression-tested by `run_corpus_qa.py`.

The corpus is *separate* from unit coverage because comprehension is a
different metric. A handler can be unit-tested green and still
misunderstand half the corpus.

**Talking point:** "How do you know your prompt engineering changes are
improvements?" — You measure on a corpus, not on vibes.

### 8. Cost engineering plan with measured baseline

**File:** [`tech_debt/backlog_token_reduction.md`](tech_debt/backlog_token_reduction.md)

A 3-phase plan to cut LLM spend 30-50% via fine-tuning, smaller models,
and prompt compression — each item annotated with a measured baseline
(tokens-per-message, $/conversation) and a projected impact.

**Talking point:** "How do you make AI products economically viable?" —
Cost is a feature; it gets a backlog and an owner.

### 9. Other production hygiene

- **Structured JSON logging** with `trace_id` correlation
  ([`app/logging_config.py`](app/logging_config.py)) — every message
  traceable end-to-end from webhook to LLM call to DB write.
- **Distributed locking** ([`app/distributed_lock.py`](app/distributed_lock.py))
  — Redis-based per-`contact_id` lock serializes cart mutations.
- **Encryption at rest** ([`app/encryption.py`](app/encryption.py))
  — Fernet for payment tokens; OAuth refresh wired with expiry tracking.
- **Channel-aware notifications** — KDS order-status changes and PIX
  expiry cron route to the correct channel (no WhatsApp send to a
  `web:{session_id}` identity).

---

## Decisions log (what I'd ask about in an interview)

### Why gpt-4o-mini and not gpt-4o, llama-3, or Claude?

- **Cost:** real-time chat at $5/1M input tokens vs gpt-4o's $2.50 (3×
  cheaper for inputs which dominate the conversation, ~3000-4000 tokens).
- **Latency:** sub-second p50 first-token critical for chat UX.
- **Tool calling:** OpenAI's native tool-calling is mature in mini;
  Llama tool calling via JSON-mode is brittle in Portuguese.
- **Eval:** corpus comprehension was 79.4% on mini vs. 81% on gpt-4o
  — not enough to justify 6× cost.

For *menu extraction from PDFs/images* (a one-shot, accuracy-sensitive
task) the model is gpt-4o, not mini. Different problem, different model.
See [`app/openai_client.py`](app/openai_client.py) `extract_products_from_image`.

### Why pgvector and not Pinecone / Weaviate / Qdrant?

- Existing Postgres. One less dependency.
- Total vector volume per bot is ~50-200 products. Doesn't justify a
  separate vector DB.
- pgvector cosine query at this scale is sub-millisecond.

If we ever cross ~10M vectors per tenant or need ANN with HNSW tuning,
this is the first thing I'd revisit.

### Why ARQ for the worker, not Celery / RQ / Dramatiq?

- Async-native (Celery's `gevent` integration is fragile with async DB drivers).
- Smaller surface area than Celery.
- Cron built in.
- One redis URL, no AMQP.

### Why Mercado Pago and not Stripe?

Brazilian market — Stripe doesn't process PIX. Mercado Pago handles
both the customer-facing PIX payment and the platform's own
SaaS subscription billing through one OAuth flow.

### Why FARGATE_SPOT in dev?

~50-70% cheaper. Acceptable for dev (tasks restart on spot reclaim,
SSE clients auto-reconnect). Production uses on-demand FARGATE.

---

## What I'd do differently with more time

Honest reflective signal — these are real, not buzzword aspirations.

1. **Prompt caching.** OpenAI's prompt-caching API would save ~50% on
   the static `system + few_shots` portion of every call. The dynamic
   `cart + menu + history` suffix prevents naive caching; would need
   to refactor to cache-friendly structure.
2. **Replace gpt-4o-mini with a fine-tuned smaller model** on the QA
   corpus. The tech-debt backlog has this as Phase 3; budget didn't
   allow a real fine-tune training run yet.
3. **Streaming responses end-to-end.** Currently buffered. Streaming
   would cut perceived latency by ~40% for long responses.
4. **Real load test.** No locust/k6 script yet. Production-ready means
   *measured* under load, not theoretical.
5. **Migrate the QA corpus to a versioned eval framework** (something
   like `inspect-ai` or a homegrown harness) — currently scoring lives
   inside the `run_corpus_qa.py` script.
6. **Build a feedback loop** — capture customer-bot conversations
   flagged by the restaurant owner as "wrong" and feed them back into
   the QA corpus. Currently the corpus only grows when I add to it
   manually.

---

## Numbers, no marketing

- **442** unit + integration tests
- **~79.4%** comprehension on the QA corpus
- **~$0.001** per conversation (measured at the per-call layer)
- **~3500** input tokens average per LLM call
- **~150** output tokens average per LLM call
- **~85%** of messages handled by the semantic router without an LLM
  intent call
- **~$0.0007** per voice transcription (Groq) / **~$0.006** (OpenAI
  fallback)
- **95** Terraform resources, **17** reusable modules
- **~$20/month** dev environment cost floor (scheduled scale-to-zero)
- **~$108/month** projected production cost at launch, **~$200/month**
  scaled to 1k restaurants
- **~$2,800/month** projected LLM spend at 1k restaurants

---

## How to inspect this repo in 10 minutes

1. **Read this file.** You're already doing it.
2. **Skim [`README.md`](README.md)** for the architecture diagram.
3. **Open [`app/semantic_router.py`](app/semantic_router.py)** — the
   piece most candidates would replace with an LLM call.
4. **Open [`app/prompt_central.py`](app/prompt_central.py)** — the
   prompt structure: system rules → few-shot examples → dynamic system
   (menu + cart + categories) → conversation history → user query.
5. **Open [`app/monitoring.py`](app/monitoring.py)** — every API call
   tracked, every cost measured.
6. **Open [`plan/in_browser_bots.md`](plan/in_browser_bots.md)** —
   the multi-channel architectural plan + the channel isolation
   contract.
7. **Open [`tech_debt/backlog_token_reduction.md`](tech_debt/backlog_token_reduction.md)**
   — the cost-engineering roadmap.
8. **Run `docker compose up`** and chat with the bot at
   `http://localhost:3000` (or wait for the public demo at
   `https://zenbotz.com.br/pizzaria-do-ze`).

---

## Want to talk?

Brazilian time zone (BRT, UTC-3), open to remote-first AI engineer
roles. Comfortable with the full stack of a production AI system but
the strongest signal is in this repo, not on a resume.

`joaoottavioc@gmail.com` · [GitHub](https://github.com/joaoottavioc)
