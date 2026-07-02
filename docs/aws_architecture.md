# ZenBots AWS Architecture

Complete reference for the ZenBots AWS infrastructure. Use this when you need to understand how things are connected, troubleshoot issues, or make manual changes.

**Last updated:** 2026-03-12

---

## Table of Contents

1. [Overview](#1-overview)
2. [Network (VPC)](#2-network-vpc)
3. [Compute (ECS Fargate)](#3-compute-ecs-fargate)
4. [Database (RDS PostgreSQL)](#4-database-rds-postgresql)
5. [Cache (ElastiCache Redis)](#5-cache-elasticache-redis)
6. [Load Balancer (ALB)](#6-load-balancer-alb)
7. [DNS and HTTPS](#7-dns-and-https)
8. [Secrets Management](#8-secrets-management)
9. [Storage (S3)](#9-storage-s3)
10. [Container Registry (ECR)](#10-container-registry-ecr)
11. [Monitoring and Alarms](#11-monitoring-and-alarms)
12. [Off-Hours Scheduling (Dev Only)](#12-off-hours-scheduling-dev-only)
13. [CI/CD Pipeline](#13-cicd-pipeline)
14. [Migrations](#14-migrations)
15. [Security Groups](#15-security-groups)
16. [Environment Comparison](#16-environment-comparison)
17. [Cost Breakdown](#17-cost-breakdown)
18. [Terraform Structure](#18-terraform-structure)
19. [Common Manual Operations](#19-common-manual-operations)
20. [Troubleshooting](#20-troubleshooting)

---

## 1. Overview

```
Internet
   │
   ▼
Route 53 (dev-api.zenbotz.com.br / api.zenbotz.com.br)
   │
   ├── [Dev]  fck-nat EIP → Caddy (HTTPS/443, Let's Encrypt) → Cloud Map → ECS backend
   └── [Prod] ALB (HTTPS/443, ACM cert) → ECS backend

ECS Fargate (private subnets)
   ├── Backend (FastAPI, port 8000)
   ├── Worker  (ARQ consumer)
   └── Redis 7 (dev only, ECS service + Cloud Map DNS)
         │
         ├──▶ RDS PostgreSQL + pgvector (isolated subnets)
         ├──▶ ElastiCache Redis (prod only, isolated subnets)
         ├──▶ S3 (menu PDFs/images, via VPC Gateway Endpoint)
         └──▶ OpenAI API (via NAT instance)
```

**Two processes** run as separate ECS services:

| Service | What it does | Container port |
|---------|-------------|----------------|
| **Backend** | FastAPI HTTP server (webhooks, API, SSE) | 8000 |
| **Worker** | ARQ async job consumer (AI, LLM, DB writes, SSE broadcast) | None |

Both share the same Docker image (different CMD) and access the same database, Redis, and secrets.

---

## 2. Network (VPC)

CIDR: `10.0.0.0/16`

### Subnets (3 tiers, 2 AZs each = 6 subnets)

| Tier | CIDRs | Purpose | Internet access |
|------|-------|---------|-----------------|
| **Public** | `10.0.1.0/24`, `10.0.2.0/24` | ALB, NAT instance | Direct (Internet Gateway) |
| **Private** | `10.0.10.0/24`, `10.0.11.0/24` | ECS tasks | Outbound only (via NAT) |
| **Isolated** | `10.0.20.0/24`, `10.0.21.0/24` | RDS (dev+prod), ElastiCache (prod only) | None |

### NAT

| Environment | Type | Cost |
|-------------|------|------|
| Dev | fck-nat t4g.nano EC2 instance | ~$3/month |
| Prod | fck-nat t4g.small EC2 instance | ~$5/month |

Both environments use ARM64 AMIs from the [fck-nat](https://github.com/AndrewGuentworker/fck-nat) project (owner `568608671756`). Each has a CloudWatch alarm for auto-recovery if the underlying host fails. Prod uses t4g.small (2 GiB RAM) for higher throughput headroom. Dev NAT runs in **us-east-1b** (us-east-1a had InsufficientInstanceCapacity for t4g.nano in March 2026).

**Caddy reverse proxy** (dev only): The NAT instance also runs Caddy for HTTPS termination (Let's Encrypt) and reverse proxy to the ECS backend via Cloud Map DNS. The Caddyfile includes `flush_interval -1` and `transport http { read_timeout 0; write_timeout 0 }` for reliable SSE streaming. See `infra/modules/nat/caddy_userdata.sh.tpl`.

> **Why not NAT Gateway?** NAT Gateway costs ~$32/mo fixed + data transfer charges. fck-nat provides the same outbound-only connectivity at ~$5/mo. Trade-off: if the fck-nat instance fails, outbound traffic (OpenAI API, WhatsApp sends) is interrupted for 2-3 minutes until CloudWatch auto-recovery restarts it. Inbound webhooks (through ALB) are NOT affected. Upgrade path: switch to NAT Gateway in `infra/environments/prod/main.tf` by changing `use_nat_instance = false`.

### S3 Gateway Endpoint

A free VPC Gateway Endpoint for S3 is attached to all private and isolated route tables. S3 traffic from ECS tasks never passes through NAT, saving data transfer costs.

---

## 3. Compute (ECS Fargate)

### Cluster

- Name: `zenbots-{env}` (e.g., `zenbots-dev`)
- Container Insights: disabled (dev, prod launch), enabled (prod scaled)
- Capacity provider: `FARGATE_SPOT` (dev), `FARGATE` (prod backend), `FARGATE_SPOT` (prod worker)

### Task Definitions

All tasks use ARM64 (Graviton) architecture for lower cost.

| Task | CPU | Memory | Image tag suffix | CMD |
|------|-----|--------|-----------------|-----|
| Backend | 1024 | 2048 | `{tag}` | `sh -c "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_WORKERS}"` (WEB_WORKERS=1, SentenceTransformer needs ~500MB/process) |
| Worker | 256 | 512 | `{tag}-worker` | `arq app.worker.WorkerSettings` (lazy imports, no model loaded) |
| Migrations | 256 | 512 | `{tag}-migrations` | `python run_migrations.py` |

### IAM Roles

Two separate roles:

| Role | Used by | Permissions |
|------|---------|------------|
| `{project}-{env}-ecs-execution` | ECS agent (pulls images, reads secrets) | `AmazonECSTaskExecutionRolePolicy` + `ssm:GetParameters` on `zenbots/{env}/*` |
| `{project}-{env}-ecs-task` | Running container (app-level AWS access) | S3 `GetObject`, `PutObject`, `DeleteObject`, `ListBucket` on the menus bucket |

### Deployment Settings

| Setting | Dev | Prod |
|---------|-----|------|
| Desired count (backend) | 1 | 1 |
| Max count (backend) | 1 | 4 (auto-scales on CPU > 70%) |
| Desired count (worker) | 1 | 1 |
| Max count (worker) | 1 | 2 |
| Worker capacity | FARGATE_SPOT | FARGATE_SPOT |
| Min healthy % | 50% | 100% (zero-downtime) |
| Max % | 200% | 200% |
| Circuit breaker | Enabled + auto-rollback | Enabled + auto-rollback |
| Worker stop timeout | 120s | 120s |

**Important:** Both services have `lifecycle { ignore_changes = [task_definition, desired_count] }` in Terraform. This means CI/CD and auto-scaling control these values, not Terraform. If you run `terraform apply`, it will NOT reset the task definition or desired count.

### Environment Variables

Non-sensitive values are passed directly as environment variables:

```
ENVIRONMENT=development      # or "production"
WEB_WORKERS=1               # Uvicorn worker count (dev=1, prod=2)
LOG_FORMAT=json
REDIS_HOST={redis endpoint}  # Dev: Cloud Map DNS (redis.zenbots-dev.local), Prod: ElastiCache endpoint
REDIS_PORT=6379
REDIS_URL=redis://{redis endpoint}:6379/0
```

Sensitive values are injected from SSM Parameter Store. See [Section 8](#8-secrets-management).

---

## 4. Database (RDS PostgreSQL)

| Setting | Dev | Prod (launch) | Prod (scaled) |
|---------|-----|---------------|----------------|
| Instance | `db.t4g.micro` | `db.t4g.micro` | `db.t4g.small` |
| Engine | PostgreSQL 14 | PostgreSQL 14 | PostgreSQL 14 |
| Database name | `botbuilder` | `botbuilder` | `botbuilder` |
| Username | `zenbots` | `zenbots` | `zenbots` |
| Storage | 20 GB (auto-scales to 50 GB) | 20 GB (auto-scales to 100 GB) | 20 GB (auto-scales to 100 GB) |
| Multi-AZ | No | Yes | Yes |
| Backup retention | 7 days | 14 days | 14 days |
| Backup window | 03:00-04:00 UTC | 03:00-04:00 UTC | 03:00-04:00 UTC |
| Maintenance window | Mon 04:00-05:00 UTC | Mon 04:00-05:00 UTC | Mon 04:00-05:00 UTC |
| Encryption | Yes (at rest) | Yes (at rest) | Yes (at rest) |
| Public access | No | No | No |
| Deletion protection | No | Yes | Yes |
| Final snapshot on destroy | Skipped | Taken | Taken |

> **Launch → Scaled upgrade trigger:** Upgrade to db.t4g.small when you have >20 paying customers or >$500/mo revenue. Multi-AZ is enabled from launch (db.t4g.micro Multi-AZ = ~$25/mo) — automatic failover in ~30 seconds with zero custom code, only $13/mo more than single-AZ.

### pgvector Extension

The `vector` extension is installed for product embedding search (384-dimensional vectors). It is created by the migration runner on first deploy via `CREATE EXTENSION IF NOT EXISTS vector`.

### Parameter Group

Custom parameter group `{project}-{env}-pg14` with:
- `shared_preload_libraries = pg_stat_statements`

### Connection String Format

```
postgresql+asyncpg://zenbots:{password}@{rds_endpoint}:5432/botbuilder
```

The app uses SQLAlchemy async with connection pooling:
- Pool size: 20
- Max overflow: 10 (up to 30 total)
- Pool pre-ping: enabled (validates connections)
- Pool recycle: 3600s (prevents stale connections)

### Accessing the Database

RDS is in isolated subnets with no public access. To connect:

1. **Via ECS exec** (recommended):
   ```bash
   # First, enable ECS Exec on the service (one-time)
   aws ecs update-service --cluster zenbots-dev \
     --service zenbots-dev-backend --enable-execute-command

   # Force a new deployment to pick up the change
   aws ecs update-service --cluster zenbots-dev \
     --service zenbots-dev-backend --force-new-deployment

   # Wait for the new task to start, then exec into it
   TASK_ARN=$(aws ecs list-tasks --cluster zenbots-dev \
     --service-name zenbots-dev-backend --query 'taskArns[0]' --output text)

   aws ecs execute-command --cluster zenbots-dev \
     --task $TASK_ARN --container backend \
     --interactive --command "/bin/bash"

   # Inside the container, use python to query:
   python -c "
   import asyncio
   from app.database import engine
   from sqlalchemy import text
   async def q():
       async with engine.connect() as c:
           r = await c.execute(text('SELECT count(*) FROM bot'))
           print(r.scalar())
   asyncio.run(q())
   "
   ```

2. **Via a one-off ECS task** with psql (more complex, see `plan/manual_steps_for_automatize_deployments.md` Step 8).

---

## 5. Cache (Redis)

### Dev: Redis on ECS Fargate

Dev uses Redis 7 Alpine as a third ECS Fargate SPOT task instead of managed ElastiCache (saves ~$10.50/mo).

| Setting | Value |
|---------|-------|
| Image | `redis:7-alpine` |
| CPU / Memory | 256 / 512 MB |
| Capacity | FARGATE_SPOT ARM64 |
| Persistence | None (`--save "" --appendonly no`) |
| Max memory | 384 MB (allkeys-lru eviction) |
| Discovery | AWS Cloud Map DNS: `redis.zenbots-dev.local` |
| Scheduling | Scales up 15 min before backend/worker, down 15 min after |
| Health check | `redis-cli ping` (10s interval, 5 retries) |

### Prod: ElastiCache Redis (managed)

| Setting | Prod (launch) | Prod (scaled) |
|---------|---------------|----------------|
| Node type | `cache.t4g.micro` | `cache.t4g.small` |
| Engine | Redis 7.0 | Redis 7.0 |
| Cluster nodes | 1 | 2 (Multi-AZ with failover) |
| Encryption at rest | Yes | Yes |
| Encryption in transit | No | No |
| Snapshot retention | None | 7 days |

> **Launch → Scaled upgrade trigger:** Enable Multi-AZ (2 nodes) and upgrade to cache.t4g.small when you have >50 restaurants or when SSE dashboard reliability is business-critical. All Redis data is ephemeral (rate limits, ARQ jobs, pub/sub). If the single node dies, ElastiCache replaces it in ~5-10 minutes. Rate limits reset and pending ARQ jobs are lost (re-enqueued on next WhatsApp message). No order data is affected.

### Redis Databases

The application uses multiple Redis databases on the same instance:

| DB | Purpose | Used by |
|----|---------|---------|
| 0 | Rate limiting | `app/rate_limiter.py` |
| 1 | ARQ job queue | `app/worker.py` |
| PubSub | SSE broadcast (`dashboard_events` channel) | `app/broadcast.py` |

### Connection

```
redis://{primary_endpoint}:6379/0
```

The `REDIS_URL` env var points to DB 0. The worker and other components adjust the DB number as needed.

---

## 6. Load Balancer (ALB)

- Name: `zenbots-{env}-alb`
- Type: Application Load Balancer (public-facing)
- Placed in: both public subnets

### Target Group

- Name: `zenbots-{env}-backend`
- Port: 8000, Protocol: HTTP
- Target type: `ip` (required for Fargate awsvpc networking)
- Deregistration delay: 30 seconds

### Health Check

| Setting | Value |
|---------|-------|
| Path | `/health/live` |
| Interval | 15 seconds |
| Timeout | 5 seconds |
| Healthy threshold | 2 consecutive checks |
| Unhealthy threshold | 3 consecutive checks |
| Expected response | HTTP 200 |

The ALB uses `/health/live` (not `/health/ready`) intentionally. `/live` always returns 200 if the process is running. `/ready` checks DB and Redis — using it for the ALB health check could remove healthy containers during transient DB issues.

### Listeners

| Port | Protocol | Action |
|------|----------|--------|
| 443 | HTTPS | Forward to target group (TLS 1.3 policy) |
| 80 | HTTP | 301 redirect to HTTPS |

The HTTPS listener uses an ACM wildcard certificate for `*.zenbotz.com.br`.

---

## 7. DNS and HTTPS

### Route 53

A single hosted zone `zenbotz.com.br` (Zone ID: `Z0672127E335XW159Q8N`) is shared by both environments:

| Record | Type | Target | Routing |
|--------|------|--------|---------|
| `dev-api.zenbotz.com.br` | A (alias) | Dev ALB | Simple |
| `api.zenbotz.com.br` (primary) | A (alias) | Prod ALB | Failover — primary |
| `api.zenbotz.com.br` (secondary) | A (alias) | S3 website endpoint (maintenance page) | Failover — secondary |

### Maintenance Page Failover (Prod)

Route 53 failover routing automatically switches `api.zenbotz.com.br` to an S3-hosted static maintenance page when the ALB is unhealthy.

**How it works:**
1. A Route 53 health check monitors the ALB via `https://api.zenbotz.com.br/health/live` (HTTPS, 30s interval, 3 failures = unhealthy)
2. When healthy → DNS resolves to ALB (primary record)
3. When unhealthy → DNS resolves to S3 website endpoint (secondary record, TTL 60s)
4. The S3 maintenance page returns a static JSON response: `{"status": "maintenance", "message": "Sistema em manutenção. Tente novamente em alguns minutos."}`
5. When ALB recovers → Route 53 switches back automatically (~60-90s propagation)

**What it covers:** ECS circuit breaker rollback (~2 min), failed deploys, complete backend crash. WhatsApp users see no response during this window (the webhook returns a non-200, Meta retries later), but any direct API consumers get a clear maintenance response instead of a timeout.

**Cost:** Route 53 health check ~$0.75/mo + S3 hosting ~$0.01/mo ≈ **$0.76/mo**.

**Terraform:** Uses the existing `maintenance-page` module in `infra/modules/maintenance-page/`.

The domain registrar (HostGator) has nameservers pointed to Route 53:
```
ns-1222.awsdns-24.org
ns-1545.awsdns-01.co.uk
ns-356.awsdns-44.com
ns-522.awsdns-01.net
```

### ACM Certificate

- ARN: `arn:aws:acm:us-east-1:578761488332:certificate/c91277cb-e5c1-463a-a8b2-75d7b613b4b1`
- Domains: `*.zenbotz.com.br` + `zenbotz.com.br`
- Validation: DNS (auto-renewing)
- Covers both `dev-api.zenbotz.com.br` and `api.zenbotz.com.br`

---

## 8. Secrets Management

### Dev: SSM Parameter Store (free)

Dev secrets were migrated from Secrets Manager to SSM Parameter Store SecureString parameters (free tier, up to 10,000 parameters). Each value is stored as an individual parameter (no JSON key extraction like Secrets Manager).

#### Parameter Layout (19 parameters)

| Parameter path | Used by |
|---------------|---------|
| `/zenbots/dev/database/url` | Backend + Worker |
| `/zenbots/dev/auth/secret_key` | Backend + Worker |
| `/zenbots/dev/auth/encryption_key` | Backend + Worker |
| `/zenbots/dev/openai/api_key` | Backend + Worker |
| `/zenbots/dev/whatsapp/token` | Backend only |
| `/zenbots/dev/whatsapp/verify_token` | Backend only |
| `/zenbots/dev/whatsapp/fb_app_id` | Backend only |
| `/zenbots/dev/whatsapp/fb_app_secret` | Backend only |
| `/zenbots/dev/mercadopago/client_id` | Backend + Worker |
| `/zenbots/dev/mercadopago/client_secret` | Backend + Worker |
| `/zenbots/dev/mercadopago/admin_token` | Backend + Worker |
| `/zenbots/dev/google/maps_api_key` | Backend + Worker |
| `/zenbots/dev/email/host` | (not injected yet) |
| `/zenbots/dev/email/port` | (not injected yet) |
| `/zenbots/dev/email/user` | (not injected yet) |
| `/zenbots/dev/email/pass` | (not injected yet) |

#### How Secrets Are Injected (Dev — SSM)

ECS task definitions reference SSM parameters using the format:

```
arn:aws:ssm:us-east-1:578761488332:parameter/zenbots/dev/database/url
```

ECS agent fetches the parameter value and sets it as the environment variable inside the container.

#### Updating a Secret (Dev)

```bash
# View current value
aws ssm get-parameter \
  --name /zenbots/dev/openai/api_key \
  --with-decryption --query 'Parameter.Value' --output text

# Update a parameter
aws ssm put-parameter \
  --name /zenbots/dev/openai/api_key \
  --value "sk-new-key-here" \
  --type SecureString --overwrite

# After updating, restart the ECS service to pick up the new value
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-backend --force-new-deployment
```

### Prod: SSM Parameter Store (free)

Prod also uses SSM Parameter Store, same structure as dev but under the `/zenbots/prod/` prefix.

#### Parameter Layout (Prod)

Same parameter names as dev (see above), with `/zenbots/prod/` prefix instead of `/zenbots/dev/`.

#### Updating a Secret (Prod)

```bash
# View current value
aws ssm get-parameter \
  --name /zenbots/prod/openai/api_key \
  --with-decryption --query 'Parameter.Value' --output text

# Update a parameter
aws ssm put-parameter \
  --name /zenbots/prod/openai/api_key \
  --value "sk-new-key-here" \
  --type SecureString --overwrite

# After updating, restart the ECS service to pick up the new value
aws ecs update-service --cluster zenbots-prod \
  --service zenbots-prod-backend --force-new-deployment
```

**Important:** ECS tasks read secrets/parameters at launch time. Changing a value does NOT affect running containers. You must force a new deployment for changes to take effect.

> **Note:** The old Secrets Manager entries for dev (`zenbots/dev/*`) still exist but are no longer referenced by ECS task definitions. They will be cleaned up in a separate PR after the confidence period.

### Critical Secrets

- **ENCRYPTION_KEY** (`auth.encryption_key`): Used by Fernet to encrypt Mercado Pago payment tokens at rest. If lost, all stored payment tokens become unrecoverable. Back this up in a password manager.
- **SECRET_KEY** (`auth.secret_key`): Used for JWT signing. Changing it invalidates all existing user sessions.

---

## 9. Storage (S3)

- Bucket name: `zenbots-{env}-menus`
- Versioning: suspended (dev), enabled (prod)
- Encryption: AES-256 (SSE-S3)
- Public access: fully blocked

Used for storing menu PDFs and images uploaded by restaurant owners. The ECS task role has `GetObject`, `PutObject`, `DeleteObject`, and `ListBucket` permissions.

Traffic to S3 goes through the VPC Gateway Endpoint (free, no NAT).

---

## 10. Container Registry (ECR)

- Repository: `zenbots/app` (single repo, shared by dev and prod)
- URL: `578761488332.dkr.ecr.us-east-1.amazonaws.com/zenbots/app`
- Scan on push: enabled
- Tag mutability: mutable (allows `latest-dev` tag updates)

### Image Tags

| Tag pattern | Environment | Example |
|-------------|-------------|---------|
| `dev-{8-char SHA}` | Dev | `dev-a1b2c3d4` |
| `prod-{8-char SHA}` | Prod | `prod-e5f6g7h8` |
| `latest-dev` | Dev (cache) | Always points to most recent dev build |
| `latest-dev-worker` | Dev (cache) | Worker image cache |
| `latest-dev-migrations` | Dev (cache) | Migrations image cache |

### Lifecycle Policy

- Untagged images: deleted after 7 days
- Tagged images: keep last 10 matching `dev-*`, `prod-*`, or `latest*`

---

## 11. Monitoring and Alarms

### SNS Topic

All alarms send to `zenbots-{env}-alerts` SNS topic, which emails `joao.ribeiro@zenbotz.com.br`.

### CloudWatch Alarms (15 total)

| Alarm | Metric | Threshold | When to worry |
|-------|--------|-----------|---------------|
| Backend CPU high | CPUUtilization | > 80% for 3 min | Scale up or optimize |
| Backend memory high | MemoryUtilization | > 85% for 3 min | Increase memory or fix leak |
| Worker CPU high | CPUUtilization | > 80% for 3 min | Add worker replicas |
| Worker memory high | MemoryUtilization | > 85% for 3 min | Increase memory |
| ALB 5xx errors | HTTPCode_Target_5XX_Count | > 10 in 2 min | App is crashing — check logs |
| ALB unhealthy hosts | UnHealthyHostCount | > 0 for 2 min | Container failing health checks |
| ALB latency high | TargetResponseTime p99 | > 5s for 3 min | Slow queries or LLM timeouts |
| **ALB request flood** | RequestCount | > 1,000 in 5 min | Possible abuse — consider enabling WAF |
| **ALB active connections** | ActiveConnectionCount | > 200 for 3 min | Connection exhaustion — possible slow HTTP attack |
| RDS CPU high | CPUUtilization | > 80% for 3 min | Upgrade instance or optimize queries |
| RDS storage low | FreeStorageSpace | < 2 GB | Increase `max_allocated_storage` |
| RDS connections high | DatabaseConnections | > 80 for 3 min | Connection leak or pool misconfiguration |
| Redis CPU high | EngineCPUUtilization | > 80% for 3 min | Upgrade node type |
| Redis memory high | DatabaseMemoryUsagePercentage | > 80% for 2 min | Eviction policy kicking in |
| Redis evictions | Evictions | > 100 in 10 min | Data being dropped — increase memory |

> **No WAF at launch (prod).** The two ALB traffic alarms above serve as an early-warning substitute. If either fires, enable WAF immediately via `enable_waf = true` in `infra/environments/prod/terraform.tfvars` and run `terraform apply` (~15 min reaction time). The app-level Redis rate limiter (`app/rate_limiter.py`) covers application-layer abuse (brute-force, credential stuffing). The ALB alarms catch network-layer floods and connection exhaustion that bypass app-level controls. See the WAF growth trigger in [Section 17](#17-cost-breakdown) for when to enable permanently.

### Log Groups

| Log group | Retention | Content |
|-----------|-----------|---------|
| `/ecs/zenbots-{env}/backend` | 3 days (dev) / 30 days (prod launch) / 90 days (prod scaled) | Backend uvicorn + app logs |
| `/ecs/zenbots-{env}/worker` | 3 days (dev) / 30 days (prod launch) / 90 days (prod scaled) | ARQ worker logs |

Logs are JSON-formatted with fields: `asctime`, `levelname`, `name`, `message`, `trace_id`, `bot_id`, `contact_id`.

---

## 12. Off-Hours Scheduling (Dev Only)

Dev services scale to zero outside business hours to save costs. RDS is stopped/started on a schedule, and Redis on ECS follows its own schedule (starts 15 min before backend/worker, stops 15 min after).

### ECS Schedule (BRT = UTC-3)

| Day | ON | OFF |
|-----|----|-----|
| Monday–Thursday | 9:00 AM | 7:00 PM |
| Friday | 9:00 AM | 7:00 PM |
| Saturday | Off all day | Off all day |
| Sunday | Off all day | Off all day |

**Running: 50 hours/week** (vs 168 hours/week 24/7)

### ECS Cron expressions (UTC)

| Action | Cron | UTC time | BRT time |
|--------|------|----------|----------|
| Scale down (Mon–Fri) | `cron(0 22 ? * MON-FRI *)` | 10:00 PM | 7:00 PM |
| Scale down (weekend) | `cron(0 22 ? * FRI *)` | 10:00 PM Fri | 7:00 PM Fri |
| Scale up (Mon–Fri) | `cron(0 12 ? * MON-FRI *)` | 12:00 PM | 9:00 AM |

No scale-up exists for Saturday/Sunday — services stay at 0. Monday's scale-up at 9 AM BRT brings them back.

### RDS Stop/Start Scheduling

RDS is stopped/started via a Lambda function triggered by EventBridge Scheduler, aligned with ECS scheduling but with 30-minute buffers.

| Action | Cron | UTC time | BRT time | Notes |
|--------|------|----------|----------|-------|
| Stop (Mon–Fri) | `cron(30 22 ? * MON-FRI *)` | 10:30 PM | 7:30 PM | 30 min after ECS scales down |
| Start (Mon–Fri) | `cron(30 11 ? * MON-FRI *)` | 11:30 AM | 8:30 AM | 30 min before ECS scales up |
| Stop (weekend) | `cron(30 22 ? * FRI *)` | 10:30 PM Fri | 7:30 PM Fri | Stays stopped until Monday |
| Start (Monday) | `cron(30 11 ? * MON *)` | 11:30 AM Mon | 8:30 AM Mon | Brings RDS back for the week |

**Lambda**: `zenbots-dev-rds-scheduler` (Python, inline code). Calls `rds.stop_db_instance()` or `rds.start_db_instance()`. Idempotent — skips if already in the desired state.

**Cost savings**: ~$12/mo → ~$4/mo (RDS only runs ~50 hrs/week).

**CI/CD integration**: The `deploy-dev.yml` pipeline checks RDS status before running migrations. If RDS is stopped, it auto-starts and waits for availability (via `rds:DescribeDBInstances` + `rds:StartDBInstance` permissions on the `github-actions-dev` IAM role).

### If you need to work outside hours

```bash
# 1. Start RDS first (takes 3-5 minutes)
aws rds start-db-instance --db-instance-identifier zenbots-dev
aws rds wait db-instance-available --db-instance-identifier zenbots-dev

# 2. Scale up Redis on ECS (must be running before backend/worker)
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-redis --desired-count 1

# 3. Wait ~30s for Redis to start, then scale up backend + worker
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-backend --desired-count 1
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-worker --desired-count 1

# The next scheduled action will automatically scale back down/up as usual
```

---

## 13. CI/CD Pipeline

### Deploy to Dev (`deploy-dev.yml`)

**Trigger:** Push to `develop` branch (ignores `*.md`, `docs/`, `tech_debt/`, `plan/`)

```
checks (lint + tests)
   │
   ▼
build-and-push (ARM64 Docker images → ECR)
   │
   ▼
migrate (ECS RunTask → run_migrations.py)
   │
   ▼
deploy (update ECS backend + worker services)
   │
   ▼
smoke-test (GET /health/ready, 10 retries × 10s)
```

- Uses OIDC (no AWS access keys stored in GitHub)
- Role: `github-actions-dev` (can only be assumed from `develop` branch or `dev` environment)
- Policies: `AmazonEC2ContainerRegistryPowerUser`, `AmazonECS_FullAccess`, `CloudWatchLogsReadOnlyAccess`, inline `rds-start-for-deploy` (DescribeDBInstances + StartDBInstance on dev RDS — enables pre-migration auto-start when RDS is stopped by off-hours scheduling)
- Pre-migration: Checks RDS status, auto-starts if stopped, waits for availability
- Concurrency: `cancel-in-progress: true` (new push cancels in-flight deploy)

### Deploy to Prod (`deploy-prod.yml`)

**Trigger:** Push to `main` branch

Same pipeline but with:
- **Manual approval gate** before migration/deploy (GitHub Environment `production` with required reviewers)
- **Pre-migration RDS snapshot** (named `pre-deploy-{datetime}`)
- **Zero-downtime deploy** (min healthy 100%)
- **No cancel-in-progress** (never cancel an active prod deploy)
- Role: `github-actions-prod`

### Terraform (`terraform.yml`)

**Trigger:** Changes to `infra/**` on PRs or pushes to `main`

- Runs `terraform plan` and posts output as PR comment
- Apply requires manual approval via GitHub Environment `infrastructure`
- Role: `github-actions-terraform`

### GitHub Secrets Required

**Repository-level:**

| Secret | Value |
|--------|-------|
| `AWS_TERRAFORM_ROLE_ARN` | `arn:aws:iam::578761488332:role/github-actions-terraform` |

**Environment: `dev`:**

| Secret | Value |
|--------|-------|
| `AWS_DEV_ROLE_ARN` | `arn:aws:iam::578761488332:role/github-actions-dev` |
| `DEV_PRIVATE_SUBNETS` | `subnet-07ebf92a924734ee2,subnet-0172c72f08c747d12` |
| `DEV_ECS_SECURITY_GROUP` | `sg-0a8aab218730ca261` |
| `DEV_API_URL` | `https://dev-api.zenbotz.com.br` |

**Environment: `production`:**

| Secret | Value |
|--------|-------|
| `AWS_PROD_ROLE_ARN` | `arn:aws:iam::578761488332:role/github-actions-prod` |
| `PROD_PRIVATE_SUBNETS` | (fill after prod terraform apply) |
| `PROD_ECS_SECURITY_GROUP` | (fill after prod terraform apply) |
| `PROD_API_URL` | `https://api.zenbotz.com.br` |

---

## 14. Migrations

Migrations run as a one-off ECS task using the `migrations` target of the Dockerfile. The entrypoint is `run_migrations.py`.

### How it works

```
run_migrations.py
   │
   ├── Fresh DB? (no alembic_version table)
   │    ├── CREATE EXTENSION IF NOT EXISTS vector
   │    ├── SQLModel.metadata.create_all()  (creates all tables)
   │    └── alembic stamp head  (marks all migrations as applied)
   │
   └── Existing DB? (alembic_version table exists)
        └── alembic upgrade head  (runs pending migrations)
```

This handles both brand new databases (creates everything from scratch) and existing databases (applies incremental migrations).

### Running migrations manually

```bash
# Run the migration task
TASK_ARN=$(aws ecs run-task \
  --cluster zenbots-dev \
  --task-definition zenbots-dev-migrations \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={
    subnets=[subnet-07ebf92a924734ee2,subnet-0172c72f08c747d12],
    securityGroups=[sg-0a8aab218730ca261],
    assignPublicIp=DISABLED
  }" \
  --query 'tasks[0].taskArn' --output text)

echo "Task: $TASK_ARN"

# Wait for it to finish
aws ecs wait tasks-stopped --cluster zenbots-dev --tasks "$TASK_ARN"

# Check exit code (0 = success)
aws ecs describe-tasks --cluster zenbots-dev --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text
```

---

## 15. Security Groups

Traffic flow between security groups:

```
Internet → [ALB SG] → [ECS SG] → [RDS SG]
                         │
                         └──────→ [Redis SG]
```

| Security Group | Inbound rules | Outbound rules |
|----------------|--------------|----------------|
| **ALB** (`zenbots-{env}-alb-sg`) | TCP 80 from `0.0.0.0/0`, TCP 443 from `0.0.0.0/0` | All traffic |
| **ECS** (`zenbots-{env}-ecs-sg`) | TCP 8000 from ALB SG | All traffic |
| **RDS** (`zenbots-{env}-rds-sg`) | TCP 5432 from ECS SG | None |
| **Redis** (`zenbots-{env}-redis-sg`) | TCP 6379 from ECS SG | None |

Key points:
- Only the ALB is internet-facing
- ECS tasks only accept traffic from the ALB on port 8000
- RDS and Redis only accept traffic from ECS tasks
- RDS and Redis have no egress rules (they don't initiate connections)

---

## 16. Environment Comparison

| Aspect | Dev | Prod (launch) | Prod (scaled) |
|--------|-----|---------------|----------------|
| Domain | `dev-api.zenbotz.com.br` | `api.zenbotz.com.br` | `api.zenbotz.com.br` |
| HTTPS termination | Caddy on fck-nat (Let's Encrypt) | ALB + ACM cert | ALB + ACM cert + WAF |
| NAT | fck-nat t4g.nano + Caddy ($3/mo) | fck-nat t4g.small ($5/mo) | fck-nat t4g.small or NAT Gateway |
| RDS | db.t4g.micro, single-AZ, scheduled | db.t4g.micro, Multi-AZ, always-on | db.t4g.small, Multi-AZ |
| Redis | Redis 7 on ECS (0.25 vCPU, 512 MB, Cloud Map DNS) | cache.t4g.micro, 1 node | cache.t4g.small, 2 nodes Multi-AZ |
| ECS backend | FARGATE_SPOT, 1 vCPU, 2 GB, 1 task, WEB_WORKERS=1 | FARGATE, 1 vCPU, 2 GB, 2-4 tasks, WEB_WORKERS=1 | FARGATE, 1 vCPU, 2 GB, 2-4 tasks |
| ECS worker | FARGATE_SPOT, 0.25 vCPU, 512 MB, 1 task | FARGATE_SPOT, 0.25 vCPU, 512 MB, 1-2 tasks | FARGATE, 0.25 vCPU, 512 MB, 1-2 tasks |
| Deploy strategy | 50% min healthy | 100% min healthy (zero-downtime) | 100% min healthy (zero-downtime) |
| Off-hours scheduling | Yes (ECS + RDS) | No (24/7) | No (24/7) |
| Secrets | SSM Parameter Store (free) | SSM Parameter Store (free) | SSM Parameter Store (free) |
| Container Insights | Disabled | Disabled | Enabled |
| WAF | No | No (app-level rate limiting) | Yes (rate limiting + managed rules) |
| CloudWatch dashboard | No | No | Yes |
| Log retention | 3 days | 30 days | 90 days |
| S3 versioning | Suspended | Enabled | Enabled |
| RDS backups | 7 days | 14 days | 14 days |
| Deletion protection | No | Yes | Yes |
| CORS | Permissive (development) | Explicit origins only | Explicit origins only |

---

## 17. Cost Breakdown

### Dev Environment (~$27/month pre-tax, ~$31 invoiced)

> Updated 2026-07-01 against actual Cost Explorer data. The old "$18-20/mo"
> figure assumed everything ran on Fargate Spot; in June 2026 all three ECS
> services were deliberately pinned to on-demand FARGATE (`base = 1`) after
> Spot reclaims + capacity shortage broke the web widget and worker. Guaranteed
> business-hours capacity costs ~$7/mo over Spot. Brazilian tax (~14%) applies
> on top of all figures below.

| Service | Cost | Notes |
|---------|------|-------|
| ECS Fargate on-demand (backend: 0.5 vCPU / 2 GB) | ~$5.06/mo | ~217 hrs/mo (scheduled), `base=1` pins to on-demand |
| ECS Fargate on-demand (worker: 0.5 vCPU / 1 GB) | ~$4.29/mo | ~217 hrs/mo (scheduled), on-demand |
| ECS Fargate on-demand (Redis: 0.25 vCPU / 512 MB) | ~$2.29/mo | ~232 hrs/mo (starts 15 min before, stops 15 min after backend) |
| RDS db.t4g.micro (compute) | ~$3.89/mo | Scheduled stop/start (~243 hrs/mo, 8:30-19:40 BRT) |
| RDS storage (20 GB gp2) + backups | ~$2.50/mo | Charged 24/7 even when instance is stopped; 1-day backup retention |
| Public IPv4 (NAT EIP) | $3.65/mo | AWS charges $0.005/hr per public IPv4 since Feb 2024 |
| NAT instance + Caddy (fck-nat t4g.nano) + EBS | ~$3.60/mo | Always on, HTTPS reverse proxy (24/7) |
| Route 53 (1 zone + queries) | ~$1.00/mo | A record → NAT EIP |
| ECR | ~$0.45/mo | Lifecycle: keep last 9 dev / 9 prod / 3 latest images (one rule per tag prefix — a combined `tagPrefixList` uses AND semantics and matches nothing) |
| CloudWatch (logs 3-day + alarms) | ~$0-1.45/mo | $0.50/GB ingestion + $0.10/alarm |
| Cloud Map (1 namespace, 2 services) | ~$0.10/mo | Redis + backend DNS discovery |
| S3 | ~$0.05/mo | <1 GB, no versioning |
| SSM Parameter Store (20 params) | $0.00 | Free tier |
| **Total** | **~$27/mo pre-tax** | ~$31/mo on the invoice with ~14% BR tax |

> **Cost optimization applied (Phases 1-4 + SM cleanup):** Secrets Manager deleted (saved ~$3.20/mo), Container Insights disabled (saved ~$1.50/mo), log retention 7→3 days (saved ~$1/mo), RDS scheduling (saved ~$8/mo), ElastiCache → Redis on ECS (saved ~$10.50/mo), ALB → Caddy on NAT (saved ~$16/mo + $7.30 IPv4). Backend runs 0.5 vCPU / 2 GB (2 GB required by the SentenceTransformer model). See `tech_debt/backlog_aws_refactor.md`.

> **Known failure mode (bit us 2026-06-08):** replacing an ECS service (e.g.
> changing its capacity provider strategy) makes AWS silently delete its
> Application Auto Scaling target and scheduled actions. Terraform does not
> detect this in the same apply — the service then runs 24/7 (~+$5/mo for
> Redis). Guards: a post-apply drift check in `terraform.yml` asserts all 9
> scheduled actions exist, and an AWS Budget (`zenbots-monthly-30`, $30/mo)
> emails at 80%/100% actual and 100% forecasted spend.

### Prod Environment — Launch Config (estimated, ~$108/month)

Applies the same cost-first philosophy proven in dev. Start lean, scale components independently via growth triggers. See `tech_debt/backlog_aws_refactor.md` for the full rationale.

| Service | Cost | Notes |
|---------|------|-------|
| ECS Fargate (backend, 1 task on-demand, 1 vCPU / 2 GB) | ~$29/mo | WEB_WORKERS=1, auto-scales to 4 on CPU > 70%. Start with 1 task, not 2. |
| RDS db.t4g.micro (Multi-AZ) | ~$25/mo | Auto-failover ~30s, upgrade to t4g.small at >20 customers |
| ALB + WAF | ~$26/mo | WAF rate limiting (2,000 req/5min/IP) |
| ElastiCache cache.t4g.micro (1 node) | ~$12/mo | Single node initially. Multi-AZ at >50 restaurants. |
| NAT (fck-nat t4g.small) | ~$5/mo | Auto-recovery alarm. Upgrade to NAT Gateway only if zero-downtime NAT is needed. |
| CloudWatch (30-day logs, 13 alarms) | ~$5/mo | No Container Insights, no dashboard initially |
| ECS Fargate (worker, 1 task Spot, 0.25 vCPU / 512 MB) | ~$3/mo | ARQ jobs retry on Spot interruption |
| Route 53 health check + S3 maintenance page | ~$2/mo | Automatic failover to static maintenance page |
| SSM Parameter Store | $0/mo | Free tier (same as dev, no Secrets Manager) |
| S3 + ECR | ~$1/mo | Menu storage |
| **Total** | **~$108/mo** | **52% less than original $223 plan** |

**Estimated availability (launch): ~99.7%** (~26 hours downtime/year). Database is fully HA with Multi-AZ automatic failover. Maintenance page failover prevents hard 503s during backend outages. Acceptable for early-stage SaaS with <20 restaurants.

### Prod Environment — Scaled Config (estimated, ~$200/month)

Triggered incrementally as the platform grows. Each upgrade is independent.

| Service | Cost | Notes |
|---------|------|-------|
| ECS Fargate (backend, 2 tasks on-demand, 1 vCPU / 2 GB each) | ~$58/mo | 2-task baseline at >20 customers, auto-scales to 4 |
| RDS db.t4g.small (Multi-AZ) | ~$50/mo | Upgraded at >20 customers or >$500/mo revenue |
| ElastiCache cache.t4g.small (2 nodes Multi-AZ) | ~$45/mo | Upgraded at >50 restaurants |
| ALB + WAF | ~$26/mo | + LCU charges under load |
| NAT (fck-nat t4g.small or Gateway) | $5-32/mo | Evaluate based on traffic |
| ECS Fargate (worker, 1 task on-demand, 0.25 vCPU / 512 MB) | ~$5/mo | Auto-scales to 2 |
| Other (SSM, Logs 90d, Dashboard, Insights) | ~$12/mo | Full observability |
| **Total** | **~$200/mo** | Full HA config |

**Estimated availability (scaled): ~99.9%** (~8.7 hours downtime/year).

### Prod Growth Triggers (when to upgrade)

| Trigger | Action | Cost impact |
|---------|--------|-------------|
| >20 paying customers | Backend baseline → 2 tasks | +$29/mo |
| >20 paying customers or >$500/mo revenue | RDS → db.t4g.small (keep Multi-AZ) | +$25/mo |
| >50 restaurants or SSE dashboard is critical | ElastiCache → cache.t4g.small Multi-AZ (2 nodes) | +$33/mo |
| ALB flood/connection alarms fire or abuse detected | Enable WAF (`enable_waf = true`) | +$10/mo |
| Full observability needed | Enable Container Insights + dashboard + 90d logs | +$5/mo |
| High outbound traffic or zero NAT downtime required | fck-nat → NAT Gateway | +$27/mo |

Each upgrade is independent and can be toggled in `infra/environments/prod/terraform.tfvars` without affecting other components.

---

## 18. Terraform Structure

### Directory Layout

```
infra/
├── global/                     # ECR + Route 53 zone (one-time)
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── backend.tf              # S3 state: global/terraform.tfstate
├── environments/
│   ├── dev/
│   │   ├── main.tf             # Wires all modules together
│   │   ├── variables.tf        # Input variable declarations
│   │   ├── terraform.tfvars    # Actual values for dev
│   │   ├── outputs.tf
│   │   └── backend.tf          # S3 state: dev/terraform.tfstate
│   └── prod/
│       ├── main.tf             # Same modules, different values
│       ├── variables.tf
│       ├── terraform.tfvars
│       ├── outputs.tf
│       └── backend.tf          # S3 state: prod/terraform.tfstate
└── modules/
    ├── vpc/                    # VPC, subnets, route tables, security groups
    ├── nat/                    # NAT instance or gateway
    ├── rds/                    # PostgreSQL on RDS
    ├── elasticache/            # Redis on ElastiCache (prod only)
    ├── redis-ecs/              # Redis 7 on ECS Fargate (dev only, replaces ElastiCache)
    ├── alb/                    # Application Load Balancer
    ├── ecs/                    # Cluster, task defs, services, IAM, auto-scaling
    ├── s3/                     # Menu storage bucket
    ├── secrets/                # SSM Parameter Store entries (legacy: Secrets Manager)
    ├── monitoring/             # SNS + CloudWatch alarms
    ├── dashboard/              # CloudWatch dashboard (prod only)
    ├── ssm-parameters/         # SSM Parameter Store (replaces Secrets Manager for dev)
    ├── scheduling/             # Off-hours ECS + Redis scaling (dev only)
    ├── rds-scheduling/         # RDS stop/start via Lambda + EventBridge (dev only)
    ├── ecr/                    # Container registry
    ├── maintenance-page/       # S3 static maintenance page (prod only)
    └── waf/                    # Web Application Firewall (prod only)
```

### State Backend

All state is stored in S3 with DynamoDB locking:

| Component | S3 key | Lock table |
|-----------|--------|------------|
| Global | `global/terraform.tfstate` | `zenbots-terraform-locks` |
| Dev | `dev/terraform.tfstate` | `zenbots-terraform-locks` |
| Prod | `prod/terraform.tfstate` | `zenbots-terraform-locks` |

Bucket: `zenbots-terraform-state`, region: `us-east-1`, versioning: enabled, encryption: KMS.

### Running Terraform Manually

```bash
# Dev environment
cd infra/environments/dev
terraform init
terraform plan -var="db_password=YOUR_DB_PASSWORD"
terraform apply -var="db_password=YOUR_DB_PASSWORD"

# Global
cd infra/global
terraform init
terraform plan
terraform apply

# Check current state
terraform show
terraform output
terraform state list
```

**Important:** The `db_password` is the only variable not in `terraform.tfvars` (for security). You must pass it via `-var` or `TF_VAR_db_password` environment variable.

---

## 19. Common Manual Operations

### Force a new deployment (pick up secret changes, restart containers)

```bash
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-backend --force-new-deployment

aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-worker --force-new-deployment
```

### Scale a service manually

```bash
# Scale backend to 2 tasks
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-backend --desired-count 2

# Scale to 0 (stop completely)
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-backend --desired-count 0
```

### View recent logs

```bash
# Backend logs (last 30 min)
MSYS_NO_PATHCONV=1 aws logs tail /ecs/zenbots-dev/backend --since 30m

# Worker logs
MSYS_NO_PATHCONV=1 aws logs tail /ecs/zenbots-dev/worker --since 30m

# Filter for errors only
MSYS_NO_PATHCONV=1 aws logs filter-log-events \
  --log-group-name /ecs/zenbots-dev/backend \
  --filter-pattern "ERROR" \
  --start-time $(date -d '1 hour ago' +%s000)
```

> **Note:** On Windows Git Bash, prefix log commands with `MSYS_NO_PATHCONV=1` to prevent path conversion of `/ecs/...`.

### Check ECS service events (deployment issues)

```bash
aws ecs describe-services --cluster zenbots-dev \
  --services zenbots-dev-backend \
  --query 'services[0].events[:10]' --output table
```

### Roll back to a previous task definition

```bash
# List recent task definition revisions
aws ecs list-task-definitions --family-prefix zenbots-dev-backend \
  --sort DESC --max-items 5

# Update service to use a specific revision
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-backend \
  --task-definition zenbots-dev-backend:REVISION_NUMBER
```

### Check what image a running task is using

```bash
TASK_ARN=$(aws ecs list-tasks --cluster zenbots-dev \
  --service-name zenbots-dev-backend --query 'taskArns[0]' --output text)

aws ecs describe-tasks --cluster zenbots-dev --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].image' --output text
```

### View Terraform outputs

```bash
cd infra/environments/dev
terraform output

# Specific output
terraform output -raw alb_dns_name
terraform output -raw rds_endpoint
```

---

## 20. Troubleshooting

### Container keeps restarting

1. Check the ECS service events:
   ```bash
   aws ecs describe-services --cluster zenbots-dev \
     --services zenbots-dev-backend \
     --query 'services[0].events[:5]' --output table
   ```
2. Check CloudWatch logs for the container (look for startup errors).
3. Common causes:
   - Secret not found (check SSM Parameter Store values for dev, Secrets Manager for prod)
   - Database unreachable (check security groups, RDS status — may be stopped by scheduling)
   - Port already in use (shouldn't happen on Fargate)

### Health check failing

1. The ALB checks `/health/live` every 15s. If 3 consecutive checks fail, the target is marked unhealthy.
2. Check if the container is running: `aws ecs describe-services ...`
3. Check container logs for startup errors.
4. Try hitting the health endpoint directly from inside the VPC (via ECS exec).

### Migration task fails

1. Check the task exit code:
   ```bash
   aws ecs describe-tasks --cluster zenbots-dev --tasks "$TASK_ARN" \
     --query 'tasks[0].containers[0].{ExitCode:exitCode,Reason:reason}' --output json
   ```
2. Check the migration logs in CloudWatch: `/ecs/zenbots-dev/backend` (migrations share the backend log group).
3. Common causes:
   - pgvector extension not installed (should be auto-handled by `run_migrations.py`)
   - Conflicting migration (check Alembic revision chain)

### Cannot connect to RDS / Redis

- These are in isolated subnets with no public access.
- Only ECS tasks (in private subnets, with the ECS security group) can reach them.
- Verify security group rules haven't been modified.
- Check that the ECS security group ID matches what's in the RDS/Redis security group inbound rules.

### Services are at 0 tasks (outside business hours)

Dev services scale to 0 at 7 PM BRT and back up at 9 AM BRT on weekdays. Weekends are fully off. RDS is stopped 30 minutes after ECS scales down. Redis on ECS stops 15 minutes after backend/worker.

To work outside hours:
```bash
# 1. Start RDS first (takes 3-5 minutes)
aws rds start-db-instance --db-instance-identifier zenbots-dev
aws rds wait db-instance-available --db-instance-identifier zenbots-dev

# 2. Scale up Redis, then backend + worker
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-redis --desired-count 1
# Wait ~30s for Redis to register in Cloud Map
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-backend --desired-count 1
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-worker --desired-count 1
```

The next scheduled action will restore normal behavior automatically.

### Migration fails because RDS is stopped

If a CI/CD deploy runs outside business hours, the migration task may fail because RDS is stopped. The `deploy-dev.yml` pipeline handles this automatically — it checks RDS status, starts it if stopped, and waits for availability before running migrations. If you're running migrations manually, start RDS first (see above).

### Pipeline fails on OIDC role assumption

- GitHub OIDC sends `repo:OWNER/REPO:environment:ENV` as the subject when the job uses `environment:` key.
- The IAM trust policy must include this subject pattern.
- Check the trust policy: `aws iam get-role --role-name github-actions-dev --query 'Role.AssumeRolePolicyDocument'`
