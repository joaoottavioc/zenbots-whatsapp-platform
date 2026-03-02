# ------------------ SNS Topic for Alerts ------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-${var.environment}-alerts"

  tags = {
    Name = "${var.project}-${var.environment}-alerts"
  }
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ------------------ ECS Alarms ------------------

resource "aws_cloudwatch_metric_alarm" "backend_cpu_high" {
  alarm_name          = "${var.project}-${var.environment}-backend-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "Backend CPU > 80% for 3 minutes"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.backend_service_name
  }
}

resource "aws_cloudwatch_metric_alarm" "backend_memory_high" {
  alarm_name          = "${var.project}-${var.environment}-backend-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "MemoryUtilization"
  namespace           = "AWS/ECS"
  period              = 60
  statistic           = "Average"
  threshold           = 85
  alarm_description   = "Backend memory > 85% for 3 minutes"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.backend_service_name
  }
}

# ------------------ ECS Worker Alarms ------------------

resource "aws_cloudwatch_metric_alarm" "worker_cpu_high" {
  alarm_name          = "${var.project}-${var.environment}-worker-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "Worker CPU > 80% for 3 minutes"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.worker_service_name
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_memory_high" {
  alarm_name          = "${var.project}-${var.environment}-worker-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "MemoryUtilization"
  namespace           = "AWS/ECS"
  period              = 60
  statistic           = "Average"
  threshold           = 85
  alarm_description   = "Worker memory > 85% for 3 minutes"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.worker_service_name
  }
}

# ------------------ ALB Alarms ------------------

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.project}-${var.environment}-alb-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "More than 10 5xx errors in 2 minutes"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }
}

# ------------------ RDS Alarms ------------------

resource "aws_cloudwatch_metric_alarm" "rds_cpu_high" {
  alarm_name          = "${var.project}-${var.environment}-rds-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "RDS CPU > 80% for 3 minutes"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    DBInstanceIdentifier = var.rds_instance_id
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_storage_low" {
  alarm_name          = "${var.project}-${var.environment}-rds-storage-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FreeStorageSpace"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 2000000000 # 2 GB
  alarm_description   = "RDS free storage < 2 GB"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    DBInstanceIdentifier = var.rds_instance_id
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_connections_high" {
  alarm_name          = "${var.project}-${var.environment}-rds-connections-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "DatabaseConnections"
  namespace           = "AWS/RDS"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "RDS connections > 80 for 3 minutes (approaching limit)"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    DBInstanceIdentifier = var.rds_instance_id
  }
}

# ------------------ ALB Additional Alarms ------------------

resource "aws_cloudwatch_metric_alarm" "alb_unhealthy_hosts" {
  alarm_name          = "${var.project}-${var.environment}-alb-unhealthy-hosts"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "UnHealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Average"
  threshold           = 0
  alarm_description   = "ALB has unhealthy targets for 2 minutes"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    TargetGroup  = var.target_group_arn_suffix
    LoadBalancer = var.alb_arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "alb_latency_high" {
  alarm_name          = "${var.project}-${var.environment}-alb-latency-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "TargetResponseTime"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "p99"
  threshold           = 5 # 5 seconds p99
  alarm_description   = "ALB p99 latency > 5s for 3 minutes"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }
}

# ------------------ ElastiCache Alarms ------------------

resource "aws_cloudwatch_metric_alarm" "redis_cpu_high" {
  alarm_name          = "${var.project}-${var.environment}-redis-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "EngineCPUUtilization"
  namespace           = "AWS/ElastiCache"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "Redis CPU > 80% for 3 minutes"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    ReplicationGroupId = var.elasticache_replication_group_id
  }
}

resource "aws_cloudwatch_metric_alarm" "redis_memory_high" {
  alarm_name          = "${var.project}-${var.environment}-redis-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "DatabaseMemoryUsagePercentage"
  namespace           = "AWS/ElastiCache"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "Redis memory > 80% for 2 minutes"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    ReplicationGroupId = var.elasticache_replication_group_id
  }
}

resource "aws_cloudwatch_metric_alarm" "redis_evictions" {
  alarm_name          = "${var.project}-${var.environment}-redis-evictions"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Evictions"
  namespace           = "AWS/ElastiCache"
  period              = 300
  statistic           = "Sum"
  threshold           = 100
  alarm_description   = "Redis evictions > 100 in 10 minutes (memory pressure)"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    ReplicationGroupId = var.elasticache_replication_group_id
  }
}
