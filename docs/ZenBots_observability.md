# ZenBots Observability API Documentation

Comprehensive documentation for frontend consumption of the ZenBots monitoring, health, and observability endpoints.

**Version:** 1.0
**Last Updated:** 2026-03-02
**Base URL:** `https://<your-domain>/`

---

## Table of Contents

1. [Authentication](#authentication)
2. [Health Endpoints](#health-endpoints)
   - [GET /health/live](#get-healthlive)
   - [GET /health/ready](#get-healthready)
3. [Monitoring Endpoints](#monitoring-endpoints)
   - [GET /monitoring/overview](#get-monitoringoverview)
   - [GET /monitoring/bot/{bot_id}](#get-monitoringbotbot_id)
   - [GET /monitoring/leaderboard](#get-monitoringleaderboard)
   - [GET /monitoring/alerts/stream (SSE)](#get-monitoringalertsstream)
4. [Data Models](#data-models)
5. [Redis Key Schema](#redis-key-schema)
6. [Cost Attribution](#cost-attribution)
7. [Anomaly Detection](#anomaly-detection)
8. [Error Codes](#error-codes)
9. [Integration Guide](#integration-guide)
10. [Architecture Overview](#architecture-overview)

---

## Authentication

All `/monitoring/*` endpoints require JWT authentication. The following methods are supported:

### Authorization Header (Recommended)

```http
GET /monitoring/overview
Authorization: Bearer <jwt_token>
```

### Obtaining a JWT Token

```http
POST /auth/token
Content-Type: application/x-www-form-urlencoded

username=user@email.com&password=yourpassword
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer"
}
```

Token expiry: **24 hours**.

### Ownership Enforcement

All monitoring endpoints filter data to **only the bots owned by the authenticated user**. A user cannot see cost data for bots they don't own. Attempting to access a bot not owned by the user returns `404 Not Found`.

---

## Health Endpoints

Health endpoints do **not** require authentication and are designed for infrastructure monitoring (load balancers, Kubernetes probes, etc.).

### GET /health/live

**Purpose:** Liveness probe. Returns 200 if the process is running.

**Response (200 OK):**
```json
{
  "status": "alive"
}
```

**Use case:** Kubernetes `livenessProbe`, load balancer health check.

---

### GET /health/ready

**Purpose:** Readiness probe. Checks all critical dependencies (PostgreSQL, Redis) and returns their status with latency measurements.

**Response (200 OK — all dependencies healthy):**
```json
{
  "status": "ready",
  "checks": {
    "postgresql": {
      "status": "up",
      "latency_ms": 2.3
    },
    "redis_db0": {
      "status": "up",
      "latency_ms": 0.8
    },
    "redis_db1_arq": {
      "status": "up",
      "latency_ms": 0.9,
      "queue_depth": 3
    },
    "monitoring_buffer": {
      "pending_events": 12
    },
    "arq_queue": {
      "queue_depth": 3,
      "status": "ok"
    }
  }
}
```

**Response (503 Service Unavailable — one or more dependencies down):**
```json
{
  "status": "degraded",
  "checks": {
    "postgresql": {
      "status": "down",
      "error": "connection refused"
    },
    "redis_db0": {
      "status": "up",
      "latency_ms": 0.8
    },
    "redis_db1_arq": {
      "status": "up",
      "latency_ms": 0.9,
      "queue_depth": 0
    },
    "monitoring_buffer": {
      "pending_events": 0
    },
    "arq_queue": {
      "queue_depth": 0,
      "status": "ok"
    }
  }
}
```

**Fields explained:**

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `"ready"` if all checks pass, `"degraded"` if any fail |
| `checks.postgresql.status` | string | `"up"` or `"down"` |
| `checks.postgresql.latency_ms` | float | Round-trip time for `SELECT 1` |
| `checks.redis_db0.status` | string | Rate limiter Redis status |
| `checks.redis_db1_arq.queue_depth` | int | Number of pending ARQ jobs |
| `checks.monitoring_buffer.pending_events` | int | Usage events waiting to be flushed to DB |
| `checks.arq_queue.queue_depth` | int | Pending worker jobs |

---

## Monitoring Endpoints

### GET /monitoring/overview

**Purpose:** Aggregate cost overview across all of the authenticated user's bots for a given time period.

**Query Parameters:**

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `days` | int | 7 | 1-90 | Number of days to aggregate |

**Request:**
```http
GET /monitoring/overview?days=7
Authorization: Bearer <jwt_token>
```

**Response (200 OK):**
```json
{
  "days": 7,
  "total_cost_usd": 42.567890,
  "services": [
    {
      "service": "openai",
      "total_cost_usd": 38.234567,
      "total_input_tokens": 25000000,
      "total_output_tokens": 1200000,
      "total_api_calls": 15420,
      "total_failed_calls": 23,
      "total_duration_ms": 4500000
    },
    {
      "service": "google_maps",
      "total_cost_usd": 2.450000,
      "total_input_tokens": 0,
      "total_output_tokens": 0,
      "total_api_calls": 490,
      "total_failed_calls": 5,
      "total_duration_ms": 98000
    },
    {
      "service": "whatsapp",
      "total_cost_usd": 1.750000,
      "total_input_tokens": 0,
      "total_output_tokens": 0,
      "total_api_calls": 35,
      "total_failed_calls": 1,
      "total_duration_ms": 7000
    },
    {
      "service": "aws_s3",
      "total_cost_usd": 0.133323,
      "total_input_tokens": 0,
      "total_output_tokens": 0,
      "total_api_calls": 12,
      "total_failed_calls": 0,
      "total_duration_ms": 2400
    }
  ],
  "bots_count": 5,
  "buffer_size": 3
}
```

**Fields explained:**

| Field | Type | Description |
|-------|------|-------------|
| `total_cost_usd` | float | Sum of all service costs in USD |
| `services` | array | Breakdown by service (openai, google_maps, aws_s3, whatsapp, etc.) |
| `services[].total_input_tokens` | int | Total LLM input tokens (0 for non-LLM services) |
| `services[].total_output_tokens` | int | Total LLM output tokens |
| `services[].total_api_calls` | int | Number of API calls made |
| `services[].total_failed_calls` | int | Number of failed API calls |
| `services[].total_duration_ms` | int | Total time spent on API calls |
| `bots_count` | int | Number of bots owned by the user |
| `buffer_size` | int | Number of events pending flush (real-time indicator) |

---

### GET /monitoring/bot/{bot_id}

**Purpose:** Detailed cost breakdown for a specific bot with daily granularity.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `bot_id` | int | The bot's unique ID |

**Query Parameters:**

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `days` | int | 7 | 1-90 | Number of days to show |

**Request:**
```http
GET /monitoring/bot/5?days=7
Authorization: Bearer <jwt_token>
```

**Response (200 OK):**
```json
{
  "bot_id": 5,
  "days": 7,
  "total_cost_usd": 15.400000,
  "daily": [
    {
      "date": "2026-03-01",
      "service": "openai",
      "total_cost_usd": 2.340000,
      "total_input_tokens": 1560000,
      "total_output_tokens": 75000,
      "total_api_calls": 1200,
      "total_failed_calls": 2,
      "total_duration_ms": 360000,
      "avg_cost_per_call": 0.00195000,
      "max_cost_single_call": 0.05000000
    },
    {
      "date": "2026-03-01",
      "service": "whatsapp",
      "total_cost_usd": 0.500000,
      "total_input_tokens": 0,
      "total_output_tokens": 0,
      "total_api_calls": 10,
      "total_failed_calls": 0,
      "total_duration_ms": 2000,
      "avg_cost_per_call": 0.05000000,
      "max_cost_single_call": 0.05000000
    }
  ],
  "realtime": {
    "today_cost_usd": 0.450000
  }
}
```

**Response (404 Not Found):**
```json
{
  "detail": "Bot not found"
}
```

**Fields explained:**

| Field | Type | Description |
|-------|------|-------------|
| `daily` | array | Cost data grouped by date + service |
| `daily[].avg_cost_per_call` | float | Average cost per API call that day |
| `daily[].max_cost_single_call` | float | Most expensive single API call that day |
| `realtime.today_cost_usd` | float | Today's cost from Redis (real-time, not yet aggregated) |

---

### GET /monitoring/leaderboard

**Purpose:** Rank the user's bots by cost for a given period. Useful for identifying the most expensive bots.

**Query Parameters:**

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `days` | int | 1 | 1-30 | Number of days to aggregate |
| `limit` | int | 10 | 1-50 | Maximum number of bots to return |

**Request:**
```http
GET /monitoring/leaderboard?days=7&limit=5
Authorization: Bearer <jwt_token>
```

**Response (200 OK):**
```json
{
  "days": 7,
  "bots": [
    {
      "rank": 1,
      "bot_id": 12,
      "restaurant_name": "Pizzaria do Ze",
      "total_cost_usd": 15.400000,
      "total_api_calls": 8200,
      "total_failed_calls": 12
    },
    {
      "rank": 2,
      "bot_id": 5,
      "restaurant_name": "Burger House",
      "total_cost_usd": 8.230000,
      "total_api_calls": 4100,
      "total_failed_calls": 3
    }
  ]
}
```

---

### GET /monitoring/alerts/stream

**Purpose:** Server-Sent Events (SSE) stream for real-time monitoring alerts. Subscribes to the `monitoring_alerts` Redis PubSub channel.

**Request:**
```http
GET /monitoring/alerts/stream
Authorization: Bearer <jwt_token>
```

**SSE Events:**

The stream emits events in standard SSE format (`data: <json>\n\n`).

#### Connection Event
Sent immediately on connect:
```
data: {"type": "connected", "message": "Monitoring alerts stream active"}
```

#### Cost Anomaly Alert
Sent when a bot's hourly cost exceeds the 7-day average by 3x (warning) or 10x (critical):
```
data: {"type": "cost_anomaly", "severity": "warning", "bot_id": 12, "service": "openai", "current_hourly_cost": 0.850000, "avg_hourly_cost": 0.250000, "ratio": 3.4, "hour": "2026030214"}
```

```
data: {"type": "cost_anomaly", "severity": "critical", "bot_id": 12, "service": "openai", "current_hourly_cost": 3.200000, "avg_hourly_cost": 0.250000, "ratio": 12.8, "hour": "2026030214"}
```

#### Keepalive
Sent every ~1 second when there are no alerts:
```
: keep-alive
```

**Alert Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Always `"cost_anomaly"` |
| `severity` | string | `"warning"` (>3x avg) or `"critical"` (>10x avg) |
| `bot_id` | int | The bot experiencing the anomaly |
| `service` | string | Which service is spiking (e.g. `"openai"`) |
| `current_hourly_cost` | float | Current hour's cost in USD |
| `avg_hourly_cost` | float | 7-day average hourly cost |
| `ratio` | float | How many times above average |
| `hour` | string | Hour key in `YYYYMMDDHH` format |

**Deduplication:** Alerts are deduplicated per bot per hour. The same bot will only trigger one alert per hour, even if cost continues to rise.

**Frontend Integration:**
```javascript
const eventSource = new EventSource('/monitoring/alerts/stream', {
  headers: { 'Authorization': `Bearer ${token}` }
});

// Note: EventSource doesn't support custom headers natively.
// Use the SSE ticket approach instead:
// 1. POST /auth/sse-ticket to get a one-time ticket
// 2. GET /monitoring/alerts/stream?ticket=<ticket>

eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  if (data.type === 'cost_anomaly') {
    showAlert(data);
  }
};
```

---

## Data Models

### UsageEvent (PostgreSQL table: `usage_events`)

Append-only log of every external API call. Each row represents one API call.

| Column | Type | Description |
|--------|------|-------------|
| `id` | int (PK) | Auto-incremented primary key |
| `bot_id` | int (FK → bot.id) | Which bot generated this call |
| `service` | string | Service identifier (see below) |
| `operation` | string | Specific function called (see below) |
| `model` | string | null | LLM model name (`"gpt-4o-mini"`, `"gpt-4o"`, or null) |
| `input_tokens` | int | Input tokens consumed (LLM only) |
| `output_tokens` | int | Output tokens generated (LLM only) |
| `cached_tokens` | int | Cached tokens (future use) |
| `cost_usd` | float | Pre-calculated cost in USD |
| `quantity` | int | Number of items (e.g., bytes for S3) |
| `duration_ms` | int | API call latency in milliseconds |
| `success` | bool | Whether the call succeeded |
| `contact_id` | int | null | Which customer triggered this |
| `trace_id` | string | null | Correlation ID for end-to-end tracing |
| `created_at` | datetime | UTC timestamp |

**Retention:** 90 days. Older events are purged by the daily aggregation cron job.

### DailyCostSummary (PostgreSQL table: `daily_cost_summary`)

Pre-aggregated daily cost data, materialized by a cron job at 3 AM UTC.

| Column | Type | Description |
|--------|------|-------------|
| `id` | int (PK) | Auto-incremented primary key |
| `bot_id` | int (FK → bot.id) | Bot this summary belongs to |
| `date` | date | UTC date |
| `service` | string | Service identifier |
| `total_cost_usd` | float | Total cost for this bot+service+day |
| `total_input_tokens` | int | Total LLM input tokens |
| `total_output_tokens` | int | Total LLM output tokens |
| `total_api_calls` | int | Total API calls |
| `total_failed_calls` | int | Total failed calls |
| `total_duration_ms` | int | Total API latency |
| `avg_cost_per_call` | float | Average cost per call |
| `max_cost_single_call` | float | Most expensive single call |

**Unique constraint:** `(bot_id, date, service)` — one row per bot per day per service.

---

## Redis Key Schema

All monitoring keys are stored in **Redis DB 2** (separate from rate limiter DB 0 and ARQ DB 1).

### Hourly Counters (48-hour TTL)
```
monitor:hourly:{bot_id}:{service}:{YYYYMMDDHH}:calls    → INT
monitor:hourly:{bot_id}:{service}:{YYYYMMDDHH}:cost     → FLOAT
monitor:hourly:{bot_id}:{service}:{YYYYMMDDHH}:tokens   → INT
```

### Daily Cost Sorted Set (8-day TTL)
```
monitor:daily_cost:{YYYYMMDD}                           → ZSET (member=bot_id, score=cost_usd)
```

### 7-Day Average (8-day TTL)
```
monitor:avg7d:{bot_id}                                  → FLOAT (average daily cost)
```

### Alert Deduplication (1-hour TTL)
```
monitor:alert_sent:{bot_id}:{YYYYMMDDHH}                → "1"
```

### Business Event Counters (8-day TTL)
```
monitor:biz:{event_name}:{YYYYMMDD}                     → INT (global counter)
monitor:biz:{event_name}:{YYYYMMDD}:{bot_id}            → INT (per-bot counter)
```

Available event names: `message_received`, `message_failed`, `order_created`, `payment_created`, `payment_completed`

### Error Counters (8-day daily / 48-hour hourly TTL)
```
monitor:errors:{source}:{YYYYMMDD}                      → INT (daily)
monitor:errors:{source}:{YYYYMMDDHH}                    → INT (hourly)
```

Sources: `openai`, `whatsapp`, `payment`, `database`

### Worker Job Metrics (8-day TTL)
```
monitor:worker:{function_name}:{YYYYMMDD}:ok            → INT
monitor:worker:{function_name}:{YYYYMMDD}:fail          → INT
```

### LLM Latency Ring Buffer (48-hour TTL)
```
monitor:latency:{function_name}                         → LIST (last 100 durations in ms)
```

### SSE Connection Counter
```
monitor:sse_connections                                  → INT (current count)
```

---

## Cost Attribution

### Service Identifiers

| Service | Description |
|---------|-------------|
| `openai` | OpenAI API calls (LLM chat, tool calling, extraction) |
| `google_maps` | Google Maps Geocoding API |
| `aws_s3` | AWS S3 file uploads |
| `whatsapp` | WhatsApp Graph API (send messages) |
| `mercado_pago` | Mercado Pago payment API |
| `facebook` | Facebook Graph API (embedded signup, WABA management) |

### Operation Identifiers

#### OpenAI Operations
| Operation | Model | Description |
|-----------|-------|-------------|
| `get_ai_decision` | gpt-4o-mini | Tool-calling for cart actions |
| `extract_potential_items` | gpt-4o-mini | Extract item names from user message |
| `classify_user_intent` | gpt-4o-mini | Fallback intent classification |
| `get_chat_response_gpt` | gpt-4o-mini | General chat response |
| `get_extraction_response` | gpt-4o | Menu data extraction from text |
| `extract_products_from_image` | gpt-4o | Menu data extraction from image |

#### Other Operations
| Operation | Service | Description |
|-----------|---------|-------------|
| `geocode` | google_maps | Address geocoding |
| `s3_put` | aws_s3 | File upload to S3 |
| `send_message` | whatsapp | Send WhatsApp message |

### Pricing Model

Costs are pre-calculated at write time using these rates:

| Model/Service | Input Cost | Output Cost |
|---------------|-----------|-------------|
| gpt-4o-mini | $0.15 / 1M tokens | $0.60 / 1M tokens |
| gpt-4o | $2.50 / 1M tokens | $10.00 / 1M tokens |
| Google Maps | $0.005 / request | — |
| S3 PUT | $0.000005 / request | — |
| WhatsApp | $0.05 / message | — |

---

## Anomaly Detection

The anomaly detection system compares real-time hourly costs against a 7-day baseline.

### How It Works

1. Every time a usage event is recorded, the system checks the current hour's cumulative cost for that bot+service.
2. It compares against `monitor:avg7d:{bot_id}` (the 7-day average daily cost, divided by 24 for hourly average).
3. If the ratio exceeds thresholds, an alert is published to `monitoring_alerts` PubSub channel.

### Thresholds

| Ratio | Severity | Action |
|-------|----------|--------|
| > 3x average | `warning` | Alert published, logged at WARNING level |
| > 10x average | `critical` | Alert published, logged at WARNING level |

### Deduplication

Each bot can only trigger **one alert per hour** (via Redis NX key with 1-hour TTL). This prevents alert storms during sustained spikes.

### Baseline Requirements

- Alerts only fire after the bot has at least one day of aggregated data (the `monitor:avg7d:{bot_id}` key must exist).
- New bots with no history will not trigger false positives.

---

## Error Codes

| Status Code | Endpoint | Meaning |
|-------------|----------|---------|
| 200 | All | Success |
| 401 | `/monitoring/*` | Missing or invalid JWT token |
| 404 | `/monitoring/bot/{bot_id}` | Bot not found or not owned by user |
| 503 | `/health/ready` | One or more dependencies are down |

---

## Integration Guide

### Polling Strategy for Dashboard

For a monitoring dashboard, we recommend:

1. **Health check:** Poll `GET /health/ready` every 30 seconds.
2. **Overview:** Poll `GET /monitoring/overview?days=7` every 60 seconds.
3. **Leaderboard:** Poll `GET /monitoring/leaderboard?days=1` every 60 seconds.
4. **Bot detail:** Load `GET /monitoring/bot/{id}?days=7` on demand when user selects a bot.
5. **Alerts:** Connect to `GET /monitoring/alerts/stream` via SSE for real-time alerts.

### Example: Fetching Overview Data

```javascript
async function fetchOverview(token, days = 7) {
  const response = await fetch(`/monitoring/overview?days=${days}`, {
    headers: { 'Authorization': `Bearer ${token}` }
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  return response.json();
}
```

### Example: Cost Chart Data

```javascript
async function fetchBotCostChart(token, botId, days = 30) {
  const response = await fetch(`/monitoring/bot/${botId}?days=${days}`, {
    headers: { 'Authorization': `Bearer ${token}` }
  });

  const data = await response.json();

  // Group by date for chart
  const dailyCosts = {};
  for (const entry of data.daily) {
    if (!dailyCosts[entry.date]) {
      dailyCosts[entry.date] = 0;
    }
    dailyCosts[entry.date] += entry.total_cost_usd;
  }

  return Object.entries(dailyCosts).map(([date, cost]) => ({
    date,
    cost: Math.round(cost * 1000000) / 1000000
  }));
}
```

### Example: Real-Time Alerts

```javascript
function connectAlertStream(token) {
  // Step 1: Get a one-time SSE ticket
  fetch('/auth/sse-ticket', {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${token}` }
  })
  .then(res => res.json())
  .then(data => {
    // Step 2: Connect to SSE with ticket
    const eventSource = new EventSource(
      `/monitoring/alerts/stream?ticket=${data.ticket}`
    );

    eventSource.onmessage = (event) => {
      const alert = JSON.parse(event.data);

      if (alert.type === 'cost_anomaly') {
        if (alert.severity === 'critical') {
          showCriticalAlert(alert);
        } else {
          showWarningAlert(alert);
        }
      }
    };

    eventSource.onerror = () => {
      // Reconnect after 5 seconds
      setTimeout(() => connectAlertStream(token), 5000);
    };
  });
}
```

---

## Architecture Overview

```
                ┌───────────────────────────────────────────────────┐
                │            Instrumentation Layer                  │
                │                                                   │
                │  openai_client.py ──┐                             │
                │  utils.py (gmaps)  ─┤                             │
                │  menu_storage.py   ─┤── record_*_usage() ──┐     │
                │  whatsapp.py (wa)  ─┤                       │     │
                │  payment_service.py─┘                       │     │
                └─────────────────────────────────────────────│─────┘
                                                              │
                                                              v
                ┌───────────────────────────────────────────────────┐
                │       In-Process Event Buffer (async list)        │
                │   Flushes every 50 events OR every 10 seconds     │
                │   Hard cap: 500 events (drops oldest)             │
                └──────────────┬─────────────────┬─────────────────┘
                               │                 │
                               v                 v
                ┌──────────────────┐  ┌────────────────────────────┐
                │   PostgreSQL     │  │     Redis (DB 2)           │
                │                  │  │                            │
                │  usage_events    │  │  Hourly counters (48h TTL) │
                │  (append-only)   │  │  Daily cost sorted sets    │
                │                  │  │  7-day averages            │
                │  daily_cost_     │  │  Business event counters   │
                │  summary (agg)   │  │  Error rate counters       │
                └────────┬─────────┘  │  Worker job metrics        │
                         │            │  PubSub: monitoring_alerts │
                         │            └──────────┬─────────────────┘
                         v                       v
                ┌───────────────────────────────────────────────────┐
                │           Monitoring API Routes                   │
                │                                                   │
                │  GET /monitoring/overview     (aggregate costs)    │
                │  GET /monitoring/bot/{id}     (per-bot detail)     │
                │  GET /monitoring/leaderboard  (top bots by cost)   │
                │  SSE /monitoring/alerts/stream (real-time alerts)  │
                │                                                   │
                │  GET /health/live             (liveness probe)     │
                │  GET /health/ready            (readiness probe)    │
                └───────────────────────────────────────────────────┘
                                      │
                                      v
                              [ Frontend Dashboard ]
```

### Data Flow

1. **Instrumentation:** Every external API call (OpenAI, Google Maps, S3, WhatsApp) records a `UsageEvent` via `record_llm_usage()` or `record_api_usage()`.
2. **Buffering:** Events are added to an in-memory buffer. When the buffer hits 50 events or 10 seconds elapse, events are bulk-inserted into PostgreSQL.
3. **Real-time counters:** Simultaneously, Redis DB 2 counters are updated via pipeline for instant dashboard access.
4. **Anomaly detection:** Each event triggers an anomaly check comparing hourly cost vs 7-day average.
5. **Daily aggregation:** A cron job at 3 AM UTC aggregates yesterday's `usage_events` into `daily_cost_summary` and recalculates 7-day averages.
6. **Purge:** Events older than 90 days are deleted during the daily aggregation job.

### Correlation / Tracing

Every WhatsApp message gets a unique 12-character hex `trace_id` (generated in `process_whatsapp_message`). This trace ID:
- Is injected into every log line via structured JSON logging
- Is stored in `usage_events.trace_id`
- Enables end-to-end tracing from webhook receipt through LLM calls to response delivery

To trace a message: search logs or query `usage_events` by `trace_id`.

---

## Appendix: Structured Log Format

All application logs are emitted in JSON format (configurable via `LOG_FORMAT=text` env var for local development).

```json
{
  "asctime": "2026-03-02T14:30:45",
  "levelname": "INFO",
  "name": "app.whatsapp",
  "message": "Message received from 5511***42 to phone_id 12345",
  "trace_id": "a1b2c3d4e5f6",
  "bot_id": 5,
  "contact_id": 42
}
```

Context fields (`trace_id`, `bot_id`, `contact_id`) are automatically injected into every log record when set in the request context.
