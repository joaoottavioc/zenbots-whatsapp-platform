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

# --- Scale DOWN at 10 PM BRT (1 AM UTC) weekdays ---

resource "aws_appautoscaling_scheduled_action" "scale_down_backend" {
  count              = var.enable_scheduling ? 1 : 0
  name               = "${var.project}-${var.environment}-backend-scale-down"
  service_namespace  = aws_appautoscaling_target.backend[0].service_namespace
  resource_id        = aws_appautoscaling_target.backend[0].resource_id
  scalable_dimension = aws_appautoscaling_target.backend[0].scalable_dimension
  schedule           = "cron(0 1 ? * MON-FRI *)" # 1 AM UTC = 10 PM BRT

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
  schedule           = "cron(0 1 ? * MON-FRI *)"

  scalable_target_action {
    min_capacity = 0
    max_capacity = 0
  }
}

# --- Scale UP at 8 AM BRT (11 AM UTC) weekdays ---

resource "aws_appautoscaling_scheduled_action" "scale_up_backend" {
  count              = var.enable_scheduling ? 1 : 0
  name               = "${var.project}-${var.environment}-backend-scale-up"
  service_namespace  = aws_appautoscaling_target.backend[0].service_namespace
  resource_id        = aws_appautoscaling_target.backend[0].resource_id
  scalable_dimension = aws_appautoscaling_target.backend[0].scalable_dimension
  schedule           = "cron(0 11 ? * MON-FRI *)" # 11 AM UTC = 8 AM BRT

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
  schedule           = "cron(0 11 ? * MON-FRI *)"

  scalable_target_action {
    min_capacity = var.worker_desired_count
    max_capacity = var.worker_desired_count
  }
}
