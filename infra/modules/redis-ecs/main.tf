# Redis on ECS Fargate — dev-only alternative to ElastiCache
# Uses Cloud Map service discovery for DNS-based access (redis.{project}-{env}.local)

# ------------------ Cloud Map Service Discovery ------------------

resource "aws_service_discovery_private_dns_namespace" "this" {
  name = "${var.project}-${var.environment}.local"
  vpc  = var.vpc_id

  tags = {
    Name = "${var.project}-${var.environment}-namespace"
  }
}

resource "aws_service_discovery_service" "redis" {
  name = "redis"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

# ------------------ Security Group Rule ------------------
# Allow ECS tasks to communicate with Redis ECS task on port 6379

resource "aws_security_group_rule" "ecs_redis_ingress" {
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = var.ecs_security_group_id
  source_security_group_id = var.ecs_security_group_id
  description              = "Allow ECS tasks to reach Redis ECS task"
}

# ------------------ CloudWatch Log Group ------------------

resource "aws_cloudwatch_log_group" "redis" {
  name              = "/ecs/${var.project}-${var.environment}/redis"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "${var.project}-${var.environment}-redis-logs"
  }
}

# ------------------ Task Definition ------------------

resource "aws_ecs_task_definition" "redis" {
  family                   = "${var.project}-${var.environment}-redis"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = var.execution_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "redis"
    image     = "redis:7-alpine"
    essential = true

    portMappings = [{
      containerPort = 6379
      protocol      = "tcp"
    }]

    # No persistence — ephemeral data only (rate limits, job queue, pub/sub)
    command = ["redis-server", "--save", "", "--appendonly", "no", "--maxmemory", "${var.maxmemory}mb", "--maxmemory-policy", "allkeys-lru"]

    healthCheck = {
      command     = ["CMD-SHELL", "redis-cli ping | grep -q PONG"]
      interval    = 15
      timeout     = 5
      retries     = 3
      startPeriod = 10
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.redis.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "redis"
      }
    }
  }])
}

# ------------------ ECS Service ------------------

resource "aws_ecs_service" "redis" {
  name            = "${var.project}-${var.environment}-redis"
  cluster         = var.cluster_name
  task_definition = aws_ecs_task_definition.redis.arn
  desired_count   = var.desired_count
  launch_type     = length(var.capacity_providers) > 0 ? null : "FARGATE"

  dynamic "capacity_provider_strategy" {
    for_each = length(var.capacity_providers) > 0 ? [1] : []
    content {
      capacity_provider = var.capacity_providers[0]
      weight            = 1
    }
  }

  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [var.ecs_security_group_id]
  }

  service_registries {
    registry_arn = aws_service_discovery_service.redis.arn
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}
