# ZenBots — Rollback Runbook

Step-by-step procedures for incident response and rollback scenarios.

---

## Quick Reference

| Scenario | Auto-Recovery? | Action | Downtime |
|----------|---------------|--------|----------|
| Migration fails | Yes (pipeline aborts) | Fix migration, re-deploy | None |
| New code fails health checks | Yes (ECS circuit breaker) | Investigate, re-deploy | ~2 min |
| New code has bugs after deploy | No | Manual ECS rollback | ~2 min |
| Database corruption | No | Restore RDS snapshot | ~15 min |
| ALB unreachable | Yes (Route 53 failover) | Investigate ALB/ECS | Users see maintenance page |

---

## 1. Migration Failure

**What happens:** The CI/CD pipeline runs `alembic upgrade head` via ECS RunTask. If the exit code is non-zero, the pipeline aborts. Old containers keep running with the old schema.

**Steps:**
1. Check the GitHub Actions workflow run for the migration logs
2. Look at CloudWatch Logs: `/ecs/zenbots-{env}` → `migrations/migrations/*`
3. Fix the migration file locally
4. Test: `alembic upgrade head` against a local database
5. Push the fix — pipeline will re-run
6. If the migration cannot be fixed quickly, consider `alembic downgrade -1`:

```bash
# Run a downgrade task (replace <CLUSTER>, <SUBNETS>, <SG> with actual values)
aws ecs run-task \
  --cluster zenbots-prod \
  --task-definition zenbots-prod-migrations \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<SUBNETS>],securityGroups=[<SG>],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"migrations","command":["alembic","downgrade","-1"]}]}'
```

---

## 2. ECS Circuit Breaker Auto-Rollback

**What happens:** If the new task definition fails health checks, ECS automatically rolls back to the last working task definition. This is handled by the deployment circuit breaker configured on both backend and worker services.

**Symptoms:**
- Deploy workflow shows "services-stable" timeout
- CloudWatch alarm: `zenbots-prod-alb-unhealthy-hosts` fires
- ECS console shows deployment status as "ROLLBACK"

**Steps:**
1. Check ECS service Events tab for rollback details
2. Check CloudWatch Logs for the failed container's startup errors
3. Fix the issue and push a new commit
4. If you need to verify the rollback happened:

```bash
aws ecs describe-services \
  --cluster zenbots-prod \
  --services zenbots-prod-backend \
  --query 'services[0].deployments'
```

---

## 3. Manual ECS Rollback

**When:** Code passes health checks but has runtime bugs (wrong business logic, data issues, etc.).

**Steps:**

### 3a. Find the previous task definition revision

```bash
# List recent revisions
aws ecs list-task-definitions \
  --family-prefix zenbots-prod-backend \
  --sort DESC \
  --max-items 5

# The current (bad) one is typically the highest number
# Roll back to the one before it
```

### 3b. Update the service to use the previous revision

```bash
# Backend
aws ecs update-service \
  --cluster zenbots-prod \
  --service zenbots-prod-backend \
  --task-definition zenbots-prod-backend:<PREVIOUS_REVISION> \
  --force-new-deployment

# Worker
aws ecs update-service \
  --cluster zenbots-prod \
  --service zenbots-prod-worker \
  --task-definition zenbots-prod-worker:<PREVIOUS_REVISION> \
  --force-new-deployment
```

### 3c. Wait for stability

```bash
aws ecs wait services-stable \
  --cluster zenbots-prod \
  --services zenbots-prod-backend zenbots-prod-worker
```

### 3d. Verify

```bash
curl -s https://api.zenbots.com.br/health/ready | python -m json.tool
```

---

## 4. RDS Snapshot Restore

**When:** Database corruption or bad data from a faulty migration that can't be fixed with `alembic downgrade`.

**Pre-requisite:** The `deploy-prod.yml` workflow creates a snapshot named `pre-deploy-YYYYMMDD-HHMMSS` before every migration.

### 4a. Find the snapshot

```bash
aws rds describe-db-snapshots \
  --db-instance-identifier zenbots-prod \
  --query 'DBSnapshots | sort_by(@, &SnapshotCreateTime) | [-5:].[DBSnapshotIdentifier, SnapshotCreateTime, Status]' \
  --output table
```

### 4b. Restore to a new instance

```bash
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier zenbots-prod-restored \
  --db-snapshot-identifier <SNAPSHOT_ID> \
  --db-instance-class db.t4g.small \
  --db-subnet-group-name zenbots-prod-db \
  --vpc-security-group-ids <RDS_SG_ID> \
  --no-multi-az
```

### 4c. Wait for the restored instance

```bash
aws rds wait db-instance-available \
  --db-instance-identifier zenbots-prod-restored
```

### 4d. Get the new endpoint

```bash
aws rds describe-db-instances \
  --db-instance-identifier zenbots-prod-restored \
  --query 'DBInstances[0].Endpoint.Address' \
  --output text
```

### 4e. Update Secrets Manager with the new endpoint

```bash
NEW_ENDPOINT=$(aws rds describe-db-instances \
  --db-instance-identifier zenbots-prod-restored \
  --query 'DBInstances[0].Endpoint.Address' \
  --output text)

aws secretsmanager update-secret \
  --secret-id zenbots/prod/database \
  --secret-string "{\"url\": \"postgresql+asyncpg://zenbots:<PASSWORD>@${NEW_ENDPOINT}:5432/botbuilder\"}"
```

### 4f. Force new ECS deployment (picks up new secret)

```bash
aws ecs update-service --cluster zenbots-prod --service zenbots-prod-backend --force-new-deployment
aws ecs update-service --cluster zenbots-prod --service zenbots-prod-worker --force-new-deployment
```

### 4g. Clean up

After confirming the restored instance is healthy:
1. Rename the old (corrupted) instance or delete it
2. Optionally rename the restored instance to `zenbots-prod`
3. Re-enable Multi-AZ on the restored instance

---

## 5. Route 53 Failover

**What happens:** Route 53 health check pings `https://api.zenbots.com.br/health/live` every 30 seconds. If it fails 3 consecutive times (90 seconds), DNS automatically switches to the S3 maintenance page.

**Recovery:** Once the ALB becomes healthy again, Route 53 automatically switches back to the primary record. No manual action needed.

**To manually test failover:**

```bash
# Check current health check status
aws route53 get-health-check-status \
  --health-check-id <HEALTH_CHECK_ID> \
  --query 'HealthCheckObservations[0].StatusReport'
```

**To force maintenance mode** (without breaking the ALB):

```bash
# Temporarily set the health check to point to a non-existent path
aws route53 update-health-check \
  --health-check-id <HEALTH_CHECK_ID> \
  --resource-path "/health/force-maintenance-mode-not-a-real-endpoint"

# To restore:
aws route53 update-health-check \
  --health-check-id <HEALTH_CHECK_ID> \
  --resource-path "/health/live"
```

---

## 6. Useful Diagnostic Commands

```bash
# ECS service status
aws ecs describe-services --cluster zenbots-prod --services zenbots-prod-backend --query 'services[0].{Status:status, Running:runningCount, Desired:desiredCount, Deployments:deployments[*].{Status:status, Running:runningCount, Desired:desiredCount, TaskDef:taskDefinition}}'

# Recent ECS events (shows deployments, health check failures)
aws ecs describe-services --cluster zenbots-prod --services zenbots-prod-backend --query 'services[0].events[:10].[createdAt, message]' --output table

# Stopped task reason (why did a container die?)
aws ecs list-tasks --cluster zenbots-prod --service-name zenbots-prod-backend --desired-status STOPPED --max-items 3
aws ecs describe-tasks --cluster zenbots-prod --tasks <TASK_ARN> --query 'tasks[0].{StopCode:stopCode, StopReason:stoppedReason, Container:containers[0].{ExitCode:exitCode, Reason:reason}}'

# CloudWatch Logs (last 30 minutes)
aws logs filter-log-events \
  --log-group-name /ecs/zenbots-prod \
  --start-time $(date -d '30 minutes ago' +%s)000 \
  --filter-pattern "ERROR" \
  --query 'events[*].[timestamp, message]' \
  --output table

# RDS status
aws rds describe-db-instances --db-instance-identifier zenbots-prod --query 'DBInstances[0].{Status:DBInstanceStatus, MultiAZ:MultiAZ, Storage:AllocatedStorage, CPU:DBInstanceClass}'

# Redis status
aws elasticache describe-replication-groups --replication-group-id zenbots-prod --query 'ReplicationGroups[0].{Status:Status, Nodes:MemberClusters, AutoFailover:AutomaticFailover}'
```

---

## 7. Escalation

If the above steps don't resolve the issue:

1. Check AWS Health Dashboard for regional issues
2. Check OpenAI Status page (if LLM calls are failing)
3. Check Meta/WhatsApp Status (if webhook delivery is failing)
4. Check Mercado Pago Status (if payments are failing)
