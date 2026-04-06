# Application Auto Scaling scheduled actions to scale ECS services to 0 outside business hours (dev only)
# Saves ~58% on dev ECS compute costs

# --- Auto Scaling Targets ---

resource "aws_appautoscaling_target" "backend" {
  count              = var.enable_scheduling ? 1 : 0
  max_capacity       = var.backend_desired_count
  min_capacity       = 0
  resource_id        = "service/${var.ecs_cluster_name}/${var.backend_service_name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_target" "worker" {
  count              = var.enable_scheduling ? 1 : 0
  max_capacity       = var.worker_desired_count
  min_capacity       = 0
  resource_id        = "service/${var.ecs_cluster_name}/${var.worker_service_name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

# --- Scale DOWN at 7 PM BRT (10 PM UTC) weekdays ---

resource "aws_appautoscaling_scheduled_action" "scale_down_backend" {
  count              = var.enable_scheduling ? 1 : 0
  name               = "${var.project}-${var.environment}-backend-scale-down"
  service_namespace  = aws_appautoscaling_target.backend[0].service_namespace
  resource_id        = aws_appautoscaling_target.backend[0].resource_id
  scalable_dimension = aws_appautoscaling_target.backend[0].scalable_dimension
  schedule           = "cron(0 22 ? * MON-FRI *)" # 10 PM UTC = 7 PM BRT

  scalable_target_action {
    min_capacity = 0
    max_capacity = 0
  }
}

resource "aws_appautoscaling_scheduled_action" "scale_down_worker" {
  count              = var.enable_scheduling ? 1 : 0
  name               = "${var.project}-${var.environment}-worker-scale-down"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  resource_id        = aws_appautoscaling_target.worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  schedule           = "cron(0 22 ? * MON-FRI *)"

  scalable_target_action {
    min_capacity = 0
    max_capacity = 0
  }
}

# --- Scale DOWN for weekends (Friday 7 PM BRT = Friday 10 PM UTC, stays off until Monday) ---

resource "aws_appautoscaling_scheduled_action" "scale_down_weekend_backend" {
  count              = var.enable_scheduling ? 1 : 0
  name               = "${var.project}-${var.environment}-backend-scale-down-weekend"
  service_namespace  = aws_appautoscaling_target.backend[0].service_namespace
  resource_id        = aws_appautoscaling_target.backend[0].resource_id
  scalable_dimension = aws_appautoscaling_target.backend[0].scalable_dimension
  schedule           = "cron(0 22 ? * FRI *)" # Friday 10 PM UTC = Friday 7 PM BRT

  scalable_target_action {
    min_capacity = 0
    max_capacity = 0
  }
}

resource "aws_appautoscaling_scheduled_action" "scale_down_weekend_worker" {
  count              = var.enable_scheduling ? 1 : 0
  name               = "${var.project}-${var.environment}-worker-scale-down-weekend"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  resource_id        = aws_appautoscaling_target.worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  schedule           = "cron(0 22 ? * FRI *)"

  scalable_target_action {
    min_capacity = 0
    max_capacity = 0
  }
}

# --- Scale UP at 9 AM BRT (12 PM UTC) Monday–Friday ---

resource "aws_appautoscaling_scheduled_action" "scale_up_backend" {
  count              = var.enable_scheduling ? 1 : 0
  name               = "${var.project}-${var.environment}-backend-scale-up"
  service_namespace  = aws_appautoscaling_target.backend[0].service_namespace
  resource_id        = aws_appautoscaling_target.backend[0].resource_id
  scalable_dimension = aws_appautoscaling_target.backend[0].scalable_dimension
  schedule           = "cron(0 12 ? * MON-FRI *)" # 12 PM UTC = 9 AM BRT

  scalable_target_action {
    min_capacity = var.backend_desired_count
    max_capacity = var.backend_desired_count
  }
}

resource "aws_appautoscaling_scheduled_action" "scale_up_worker" {
  count              = var.enable_scheduling ? 1 : 0
  name               = "${var.project}-${var.environment}-worker-scale-up"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  resource_id        = aws_appautoscaling_target.worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  schedule           = "cron(0 12 ? * MON-FRI *)"

  scalable_target_action {
    min_capacity = var.worker_desired_count
    max_capacity = var.worker_desired_count
  }
}

# ============================================================
# Redis ECS service scheduling (optional, dev only)
# Redis must start BEFORE backend/worker and stop AFTER them
# ============================================================

locals {
  enable_redis_scheduling = var.enable_scheduling && var.redis_service_name != ""
}

resource "aws_appautoscaling_target" "redis" {
  count              = local.enable_redis_scheduling ? 1 : 0
  max_capacity       = var.redis_desired_count
  min_capacity       = 0
  resource_id        = "service/${var.ecs_cluster_name}/${var.redis_service_name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

# Scale DOWN Redis 15 min AFTER backend/worker (10:15 PM UTC)
resource "aws_appautoscaling_scheduled_action" "scale_down_redis" {
  count              = local.enable_redis_scheduling ? 1 : 0
  name               = "${var.project}-${var.environment}-redis-scale-down"
  service_namespace  = aws_appautoscaling_target.redis[0].service_namespace
  resource_id        = aws_appautoscaling_target.redis[0].resource_id
  scalable_dimension = aws_appautoscaling_target.redis[0].scalable_dimension
  schedule           = "cron(15 22 ? * MON-FRI *)"

  scalable_target_action {
    min_capacity = 0
    max_capacity = 0
  }
}

resource "aws_appautoscaling_scheduled_action" "scale_down_weekend_redis" {
  count              = local.enable_redis_scheduling ? 1 : 0
  name               = "${var.project}-${var.environment}-redis-scale-down-weekend"
  service_namespace  = aws_appautoscaling_target.redis[0].service_namespace
  resource_id        = aws_appautoscaling_target.redis[0].resource_id
  scalable_dimension = aws_appautoscaling_target.redis[0].scalable_dimension
  schedule           = "cron(15 22 ? * FRI *)"

  scalable_target_action {
    min_capacity = 0
    max_capacity = 0
  }
}

# Scale UP Redis 15 min BEFORE backend/worker (11:45 AM UTC)
resource "aws_appautoscaling_scheduled_action" "scale_up_redis" {
  count              = local.enable_redis_scheduling ? 1 : 0
  name               = "${var.project}-${var.environment}-redis-scale-up"
  service_namespace  = aws_appautoscaling_target.redis[0].service_namespace
  resource_id        = aws_appautoscaling_target.redis[0].resource_id
  scalable_dimension = aws_appautoscaling_target.redis[0].scalable_dimension
  schedule           = "cron(45 11 ? * MON-FRI *)"

  scalable_target_action {
    min_capacity = var.redis_desired_count
    max_capacity = var.redis_desired_count
  }
}
