# EventBridge rules to scale ECS services to 0 outside business hours (dev only)
# Saves ~58% on dev ECS compute costs

# --- Scale DOWN at 10 PM BRT (1 AM UTC) ---

resource "aws_cloudwatch_event_rule" "scale_down" {
  count               = var.enable_scheduling ? 1 : 0
  name                = "${var.project}-${var.environment}-scale-down"
  description         = "Scale ECS to 0 at 10 PM BRT (weekdays)"
  schedule_expression = "cron(0 1 ? * MON-FRI *)" # 1 AM UTC = 10 PM BRT

  tags = {
    Name = "${var.project}-${var.environment}-scale-down"
  }
}

resource "aws_cloudwatch_event_target" "scale_down_backend" {
  count     = var.enable_scheduling ? 1 : 0
  rule      = aws_cloudwatch_event_rule.scale_down[0].name
  target_id = "scale-down-backend"
  arn       = "arn:aws:ecs:${var.region}:${var.account_id}:service/${var.ecs_cluster_name}/${var.backend_service_name}"
  role_arn  = aws_iam_role.eventbridge[0].arn

  ecs_target {
    task_count = 0
  }
}

resource "aws_cloudwatch_event_target" "scale_down_worker" {
  count     = var.enable_scheduling ? 1 : 0
  rule      = aws_cloudwatch_event_rule.scale_down[0].name
  target_id = "scale-down-worker"
  arn       = "arn:aws:ecs:${var.region}:${var.account_id}:service/${var.ecs_cluster_name}/${var.worker_service_name}"
  role_arn  = aws_iam_role.eventbridge[0].arn

  ecs_target {
    task_count = 0
  }
}

# --- Scale UP at 8 AM BRT (11 AM UTC) ---

resource "aws_cloudwatch_event_rule" "scale_up" {
  count               = var.enable_scheduling ? 1 : 0
  name                = "${var.project}-${var.environment}-scale-up"
  description         = "Scale ECS back up at 8 AM BRT (weekdays)"
  schedule_expression = "cron(0 11 ? * MON-FRI *)" # 11 AM UTC = 8 AM BRT

  tags = {
    Name = "${var.project}-${var.environment}-scale-up"
  }
}

resource "aws_cloudwatch_event_target" "scale_up_backend" {
  count     = var.enable_scheduling ? 1 : 0
  rule      = aws_cloudwatch_event_rule.scale_up[0].name
  target_id = "scale-up-backend"
  arn       = "arn:aws:ecs:${var.region}:${var.account_id}:service/${var.ecs_cluster_name}/${var.backend_service_name}"
  role_arn  = aws_iam_role.eventbridge[0].arn

  ecs_target {
    task_count = var.backend_desired_count
  }
}

resource "aws_cloudwatch_event_target" "scale_up_worker" {
  count     = var.enable_scheduling ? 1 : 0
  rule      = aws_cloudwatch_event_rule.scale_up[0].name
  target_id = "scale-up-worker"
  arn       = "arn:aws:ecs:${var.region}:${var.account_id}:service/${var.ecs_cluster_name}/${var.worker_service_name}"
  role_arn  = aws_iam_role.eventbridge[0].arn

  ecs_target {
    task_count = var.worker_desired_count
  }
}

# --- IAM Role for EventBridge ---

resource "aws_iam_role" "eventbridge" {
  count = var.enable_scheduling ? 1 : 0
  name  = "${var.project}-${var.environment}-eventbridge-ecs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "events.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_policy" "eventbridge_ecs" {
  count = var.enable_scheduling ? 1 : 0
  name  = "${var.project}-${var.environment}-eventbridge-ecs"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ecs:UpdateService"]
      Resource = "*"
      Condition = {
        ArnEquals = {
          "ecs:cluster" = "arn:aws:ecs:${var.region}:${var.account_id}:cluster/${var.ecs_cluster_name}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eventbridge_ecs" {
  count      = var.enable_scheduling ? 1 : 0
  role       = aws_iam_role.eventbridge[0].name
  policy_arn = aws_iam_policy.eventbridge_ecs[0].arn
}
