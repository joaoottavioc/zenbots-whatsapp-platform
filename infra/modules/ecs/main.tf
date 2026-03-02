# ------------------ ECS Cluster ------------------

resource "aws_ecs_cluster" "this" {
  name = "${var.project}-${var.environment}"

  setting {
    name  = "containerInsights"
    value = var.container_insights ? "enabled" : "disabled"
  }

  tags = {
    Name = "${var.project}-${var.environment}-cluster"
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name = aws_ecs_cluster.this.name

  capacity_providers = var.capacity_providers

  default_capacity_provider_strategy {
    capacity_provider = var.capacity_providers[0]
    weight            = 1
  }
}

# ------------------ IAM Roles ------------------

# Task Execution Role — ECS agent uses this to pull images, read secrets
resource "aws_iam_role" "execution" {
  name = "${var.project}-${var.environment}-ecs-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_policy" "secrets_read" {
  name = "${var.project}-${var.environment}-secrets-read"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = "arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:${var.project}/${var.environment}/*"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "secrets_read" {
  role       = aws_iam_role.execution.name
  policy_arn = aws_iam_policy.secrets_read.arn
}

# Task Role — the application container uses this for S3, etc.
resource "aws_iam_role" "task" {
  name = "${var.project}-${var.environment}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_policy" "s3_access" {
  name = "${var.project}-${var.environment}-s3-access"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
      ]
      Resource = [
        "arn:aws:s3:::${var.s3_bucket_name}",
        "arn:aws:s3:::${var.s3_bucket_name}/*",
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "s3_access" {
  role       = aws_iam_role.task.name
  policy_arn = aws_iam_policy.s3_access.arn
}

# ------------------ CloudWatch Log Groups ------------------

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/ecs/${var.project}-${var.environment}/backend"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${var.project}-${var.environment}/worker"
  retention_in_days = var.log_retention_days
}

# ------------------ Task Definitions ------------------

resource "aws_ecs_task_definition" "backend" {
  family                   = "${var.project}-${var.environment}-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.backend_cpu
  memory                   = var.backend_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "backend"
    image     = var.backend_image
    essential = true

    portMappings = [{
      containerPort = 8000
      protocol      = "tcp"
    }]

    environment = var.backend_environment
    secrets     = var.backend_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.backend.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "backend"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.project}-${var.environment}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = var.worker_image
    essential = true

    stopTimeout = var.worker_stop_timeout

    environment = var.worker_environment
    secrets     = var.worker_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "migrations" {
  family                   = "${var.project}-${var.environment}-migrations"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "migrations"
    image     = var.migrations_image
    essential = true

    environment = var.backend_environment
    secrets     = var.backend_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.backend.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "migrations"
      }
    }
  }])
}

# ------------------ ECS Services ------------------

resource "aws_ecs_service" "backend" {
  name            = "${var.project}-${var.environment}-backend"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = var.backend_desired_count
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

  load_balancer {
    target_group_arn = var.target_group_arn
    container_name   = "backend"
    container_port   = 8000
  }

  deployment_configuration {
    minimum_healthy_percent = var.minimum_healthy_percent
    maximum_percent         = var.maximum_percent

    deployment_circuit_breaker {
      enable   = true
      rollback = true
    }
  }

  dynamic "ordered_placement_strategy" {
    for_each = var.environment == "prod" ? [1] : []
    content {
      type  = "spread"
      field = "attribute:ecs.availability-zone"
    }
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

resource "aws_ecs_service" "worker" {
  name            = "${var.project}-${var.environment}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
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

  deployment_configuration {
    minimum_healthy_percent = var.minimum_healthy_percent
    maximum_percent         = var.maximum_percent

    deployment_circuit_breaker {
      enable   = true
      rollback = true
    }
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

# ------------------ Auto Scaling ------------------

resource "aws_appautoscaling_target" "backend" {
  count              = var.backend_max_count > var.backend_desired_count ? 1 : 0
  max_capacity       = var.backend_max_count
  min_capacity       = var.backend_desired_count
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.backend.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "backend_cpu" {
  count              = var.backend_max_count > var.backend_desired_count ? 1 : 0
  name               = "${var.project}-${var.environment}-backend-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.backend[0].resource_id
  scalable_dimension = aws_appautoscaling_target.backend[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.backend[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

resource "aws_appautoscaling_target" "worker" {
  count              = var.worker_max_count > var.worker_desired_count ? 1 : 0
  max_capacity       = var.worker_max_count
  min_capacity       = var.worker_desired_count
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "worker_cpu" {
  count              = var.worker_max_count > var.worker_desired_count ? 1 : 0
  name               = "${var.project}-${var.environment}-worker-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}
