# ZenBots AWS Architecture

Complete reference for the ZenBots AWS infrastructure. Use this when you need to understand how things are connected, troubleshoot issues, or make manual changes.

**Last updated:** 2026-03-03

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
Route 53 (dev-api.zenbotz.com.br)
   │
   ▼
ALB (public subnets, ports 80→443 redirect, 443→backend)
   │
   ▼
ECS Fargate (private subnets)
   ├── Backend (FastAPI, port 8000)
   └── Worker  (ARQ consumer)
         │
         ├──▶ RDS PostgreSQL + pgvector (isolated subnets)
         ├──▶ ElastiCache Redis (isolated subnets)
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
| **Isolated** | `10.0.20.0/24`, `10.0.21.0/24` | RDS, ElastiCache | None |

### NAT

| Environment | Type | Cost |
|-------------|------|------|
| Dev | fck-nat t4g.nano EC2 instance | ~$3/month |
| Prod | Managed NAT Gateway | ~$32/month |

The NAT instance in dev uses an ARM64 AMI from the [fck-nat](https://github.com/AndrewGuentworker/fck-nat) project (owner `568608671756`). It has a CloudWatch alarm for auto-recovery if the underlying host fails.

### S3 Gateway Endpoint

A free VPC Gateway Endpoint for S3 is attached to all private and isolated route tables. S3 traffic from ECS tasks never passes through NAT, saving data transfer costs.

---

## 3. Compute (ECS Fargate)

### Cluster

- Name: `zenbots-{env}` (e.g., `zenbots-dev`)
- Container Insights: enabled
- Capacity provider: `FARGATE_SPOT` (dev), `FARGATE` (prod)

### Task Definitions

All tasks use ARM64 (Graviton) architecture for lower cost.

| Task | CPU | Memory | Image tag suffix | CMD |
|------|-----|--------|-----------------|-----|
| Backend | 256 (dev) / 512 (prod) | 512 (dev) / 1024 (prod) | `{tag}` | `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2` |
| Worker | 256 (dev) / 512 (prod) | 512 (dev) / 1024 (prod) | `{tag}-worker` | `arq app.worker.WorkerSettings` |
| Migrations | 256 | 512 | `{tag}-migrations` | `python run_migrations.py` |

### IAM Roles

Two separate roles:

| Role | Used by | Permissions |
|------|---------|------------|
| `{project}-{env}-ecs-execution` | ECS agent (pulls images, reads secrets) | `AmazonECSTaskExecutionRolePolicy` + `secretsmanager:GetSecretValue` on `zenbots/{env}/*` |
| `{project}-{env}-ecs-task` | Running container (app-level AWS access) | S3 `GetObject`, `PutObject`, `DeleteObject`, `ListBucket` on the menus bucket |

### Deployment Settings

| Setting | Dev | Prod |
|---------|-----|------|
| Desired count (backend) | 1 | 2 |
| Max count (backend) | 1 | 4 (auto-scales on CPU > 70%) |
| Desired count (worker) | 1 | 1 |
| Max count (worker) | 1 | 2 |
| Min healthy % | 50% | 100% (zero-downtime) |
| Max % | 200% | 200% |
| Circuit breaker | Enabled + auto-rollback | Enabled + auto-rollback |
| Worker stop timeout | 120s | 120s |

**Important:** Both services have `lifecycle { ignore_changes = [task_definition, desired_count] }` in Terraform. This means CI/CD and auto-scaling control these values, not Terraform. If you run `terraform apply`, it will NOT reset the task definition or desired count.

### Environment Variables

Non-sensitive values are passed directly as environment variables:

```
ENVIRONMENT=development      # or "production"
LOG_FORMAT=json
REDIS_HOST={elasticache endpoint}
REDIS_PORT=6379
REDIS_URL=redis://{elasticache endpoint}:6379/0
```

Sensitive values are injected from Secrets Manager (see [Section 8](#8-secrets-management)).

---

## 4. Database (RDS PostgreSQL)

| Setting | Dev | Prod |
|---------|-----|------|
| Instance | `db.t4g.micro` | `db.t4g.small` |
| Engine | PostgreSQL 14 | PostgreSQL 14 |
| Database name | `botbuilder` | `botbuilder` |
| Username | `zenbots` | `zenbots` |
| Storage | 20 GB (auto-scales to 50 GB) | 20 GB (auto-scales to 100 GB) |
| Multi-AZ | No | Yes |
| Backup retention | 7 days | 14 days |
| Backup window | 03:00-04:00 UTC | 03:00-04:00 UTC |
| Maintenance window | Mon 04:00-05:00 UTC | Mon 04:00-05:00 UTC |
| Encryption | Yes (at rest) | Yes (at rest) |
| Public access | No | No |
| Deletion protection | No | Yes |
| Final snapshot on destroy | Skipped | Taken |

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

## 5. Cache (ElastiCache Redis)

| Setting | Dev | Prod |
|---------|-----|------|
| Node type | `cache.t4g.micro` | `cache.t4g.small` |
| Engine | Redis 7.0 | Redis 7.0 |
| Cluster nodes | 1 | 2 (Multi-AZ with failover) |
| Encryption at rest | Yes | Yes |
| Encryption in transit | No | No |
| Snapshot retention | None | 7 days |

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

| Record | Type | Target |
|--------|------|--------|
| `dev-api.zenbotz.com.br` | A (alias) | Dev ALB |
| `api.zenbotz.com.br` | A (alias) | Prod ALB (when deployed) |

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

All secrets are stored in AWS Secrets Manager under the path `zenbots/{env}/{name}`.

### Secret Layout

| Secret path | JSON fields | Used by |
|-------------|-------------|---------|
| `zenbots/{env}/database` | `url` | Backend + Worker |
| `zenbots/{env}/redis` | `url` | (created but not injected — Redis passed as plain env vars) |
| `zenbots/{env}/auth` | `secret_key`, `encryption_key` | Backend + Worker |
| `zenbots/{env}/openai` | `api_key` | Backend + Worker |
| `zenbots/{env}/whatsapp` | `token`, `verify_token`, `fb_app_id`, `fb_app_secret` | Backend only |
| `zenbots/{env}/mercadopago` | `client_id`, `client_secret`, `admin_token` | Backend + Worker |
| `zenbots/{env}/google` | `maps_api_key` | Backend + Worker |
| `zenbots/{env}/email` | `host`, `port`, `user`, `pass` | (created but not injected yet) |

### How Secrets Are Injected

ECS task definitions reference secrets using the format:

```
{secret_arn}:{json_field}::
```

For example, `DATABASE_URL` is injected as:
```
arn:aws:secretsmanager:us-east-1:578761488332:secret:zenbots/dev/database-m0GaVC:url::
```

ECS agent fetches the secret, extracts the `url` field from the JSON, and sets it as the `DATABASE_URL` environment variable inside the container.

### Updating a Secret

```bash
# View current value
aws secretsmanager get-secret-value \
  --secret-id zenbots/dev/openai \
  --query 'SecretString' --output text | python -m json.tool

# Update a specific field (preserves other fields)
CURRENT=$(aws secretsmanager get-secret-value \
  --secret-id zenbots/dev/openai --query 'SecretString' --output text)

echo $CURRENT | python -c "
import sys, json
d = json.load(sys.stdin)
d['api_key'] = 'sk-new-key-here'
print(json.dumps(d))
" | xargs -0 aws secretsmanager update-secret \
  --secret-id zenbots/dev/openai --secret-string

# After updating, restart the ECS service to pick up the new value
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-backend --force-new-deployment
```

**Important:** ECS tasks read secrets at launch time. Changing a secret in Secrets Manager does NOT affect running containers. You must force a new deployment for changes to take effect.

### Critical Secrets

- **ENCRYPTION_KEY** (`auth.encryption_key`): Used by Fernet to encrypt Mercado Pago payment tokens at rest. If lost, all stored payment tokens become unrecoverable. Back this up in a password manager.
- **SECRET_KEY** (`auth.secret_key`): Used for JWT signing. Changing it invalidates all existing user sessions.

---

## 9. Storage (S3)

- Bucket name: `zenbots-{env}-menus`
- Versioning: enabled
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

### CloudWatch Alarms (13 total)

| Alarm | Metric | Threshold | When to worry |
|-------|--------|-----------|---------------|
| Backend CPU high | CPUUtilization | > 80% for 3 min | Scale up or optimize |
| Backend memory high | MemoryUtilization | > 85% for 3 min | Increase memory or fix leak |
| Worker CPU high | CPUUtilization | > 80% for 3 min | Add worker replicas |
| Worker memory high | MemoryUtilization | > 85% for 3 min | Increase memory |
| ALB 5xx errors | HTTPCode_Target_5XX_Count | > 10 in 2 min | App is crashing — check logs |
| ALB unhealthy hosts | UnHealthyHostCount | > 0 for 2 min | Container failing health checks |
| ALB latency high | TargetResponseTime p99 | > 5s for 3 min | Slow queries or LLM timeouts |
| RDS CPU high | CPUUtilization | > 80% for 3 min | Upgrade instance or optimize queries |
| RDS storage low | FreeStorageSpace | < 2 GB | Increase `max_allocated_storage` |
| RDS connections high | DatabaseConnections | > 80 for 3 min | Connection leak or pool misconfiguration |
| Redis CPU high | EngineCPUUtilization | > 80% for 3 min | Upgrade node type |
| Redis memory high | DatabaseMemoryUsagePercentage | > 80% for 2 min | Eviction policy kicking in |
| Redis evictions | Evictions | > 100 in 10 min | Data being dropped — increase memory |

### Log Groups

| Log group | Retention | Content |
|-----------|-----------|---------|
| `/ecs/zenbots-{env}/backend` | 7 days (dev) / 90 days (prod) | Backend uvicorn + app logs |
| `/ecs/zenbots-{env}/worker` | 7 days (dev) / 90 days (prod) | ARQ worker logs |

Logs are JSON-formatted with fields: `asctime`, `levelname`, `name`, `message`, `trace_id`, `bot_id`, `contact_id`.

---

## 12. Off-Hours Scheduling (Dev Only)

Dev services scale to zero outside business hours to save costs.

### Schedule (BRT = UTC-3)

| Day | ON | OFF |
|-----|----|-----|
| Monday–Thursday | 9:00 AM | 7:00 PM |
| Friday | 9:00 AM | 7:00 PM |
| Saturday | Off all day | Off all day |
| Sunday | Off all day | Off all day |

**Running: 50 hours/week** (vs 168 hours/week 24/7)

### Cron expressions (UTC)

| Action | Cron | UTC time | BRT time |
|--------|------|----------|----------|
| Scale down (Mon–Fri) | `cron(0 22 ? * MON-FRI *)` | 10:00 PM | 7:00 PM |
| Scale down (weekend) | `cron(0 22 ? * FRI *)` | 10:00 PM Fri | 7:00 PM Fri |
| Scale up (Mon–Fri) | `cron(0 12 ? * MON-FRI *)` | 12:00 PM | 9:00 AM |

No scale-up exists for Saturday/Sunday — services stay at 0. Monday's scale-up at 9 AM BRT brings them back.

### If you need to work outside hours

```bash
# Manually scale up
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

| Aspect | Dev | Prod |
|--------|-----|------|
| Domain | `dev-api.zenbotz.com.br` | `api.zenbotz.com.br` |
| NAT | fck-nat instance ($3/mo) | Managed NAT Gateway ($32/mo) |
| RDS | db.t4g.micro, single-AZ | db.t4g.small, Multi-AZ |
| Redis | cache.t4g.micro, 1 node | cache.t4g.small, 2 nodes |
| ECS capacity | FARGATE_SPOT | FARGATE (on-demand) |
| Backend | 0.25 vCPU, 512 MB, 1 task | 0.5 vCPU, 1 GB, 2-4 tasks |
| Worker | 0.25 vCPU, 512 MB, 1 task | 0.5 vCPU, 1 GB, 1-2 tasks |
| Deploy strategy | 50% min healthy | 100% min healthy (zero-downtime) |
| Off-hours scheduling | Yes (7PM-9AM + weekends) | No (24/7) |
| WAF | No | Yes (rate limiting + managed rules) |
| CloudWatch dashboard | No | Yes |
| Log retention | 7 days | 90 days |
| RDS backups | 7 days | 14 days |
| Deletion protection | No | Yes |
| CORS | Permissive (development) | Explicit origins only |

---

## 17. Cost Breakdown

### Dev Environment (~$44/month)

| Service | Always on | With scheduling | Notes |
|---------|-----------|-----------------|-------|
| RDS db.t4g.micro | $12/mo | $12/mo | Cannot be scheduled |
| ElastiCache cache.t4g.micro | $12/mo | $12/mo | Cannot be scheduled |
| NAT instance (t4g.nano) | $3/mo | $3/mo | Always runs |
| ECS Fargate Spot (backend) | $5/mo | ~$1.50/mo | 50h/week vs 168h |
| ECS Fargate Spot (worker) | $5/mo | ~$1.50/mo | 50h/week vs 168h |
| ALB | $16/mo | $16/mo | Always runs |
| S3 + Secrets Manager + CloudWatch | ~$2/mo | ~$2/mo | Minimal usage |
| **Total** | **~$55/mo** | **~$48/mo** | |

### Prod Environment (estimated, ~$200-275/month)

| Service | Cost | Notes |
|---------|------|-------|
| RDS db.t4g.small (Multi-AZ) | $50/mo | 2 instances |
| ElastiCache cache.t4g.small (2 nodes) | $45/mo | Multi-AZ |
| NAT Gateway | $32/mo | + data transfer |
| ECS Fargate (backend, 2 tasks) | $40/mo | On-demand pricing |
| ECS Fargate (worker, 1 task) | $20/mo | |
| ALB | $16/mo | + LCU charges |
| WAF | $10/mo | Web ACL + rules |
| Other (S3, Secrets, Logs, Dashboard) | $10/mo | |
| **Total** | **~$223/mo** | Without auto-scaling |

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
    ├── elasticache/            # Redis on ElastiCache
    ├── alb/                    # Application Load Balancer
    ├── ecs/                    # Cluster, task defs, services, IAM, auto-scaling
    ├── s3/                     # Menu storage bucket
    ├── secrets/                # Secrets Manager entries
    ├── monitoring/             # SNS + CloudWatch alarms
    ├── dashboard/              # CloudWatch dashboard (prod only)
    ├── scheduling/             # Off-hours scaling (dev only)
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
   - Secret not found (check Secrets Manager values)
   - Database unreachable (check security groups, RDS status)
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

Dev services scale to 0 at 7 PM BRT and back up at 9 AM BRT on weekdays. Weekends are fully off.

To work outside hours:
```bash
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-backend --desired-count 1
aws ecs update-service --cluster zenbots-dev \
  --service zenbots-dev-worker --desired-count 1
```

The next scheduled action will restore normal behavior automatically.

### Pipeline fails on OIDC role assumption

- GitHub OIDC sends `repo:OWNER/REPO:environment:ENV` as the subject when the job uses `environment:` key.
- The IAM trust policy must include this subject pattern.
- Check the trust policy: `aws iam get-role --role-name github-actions-dev --query 'Role.AssumeRolePolicyDocument'`
