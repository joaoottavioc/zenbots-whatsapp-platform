resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.project}-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [

      # ── Row 1: ECS Backend ────────────────────────────
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 1
        properties = {
          markdown = "# ECS — Backend"
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 1
        width  = 8
        height = 6
        properties = {
          title   = "Backend CPU Utilization"
          region  = var.region
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", var.ecs_cluster_name, "ServiceName", var.backend_service_name, { stat = "Average" }]
          ]
          period = 60
          yAxis  = { left = { min = 0, max = 100 } }
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 1
        width  = 8
        height = 6
        properties = {
          title   = "Backend Memory Utilization"
          region  = var.region
          metrics = [
            ["AWS/ECS", "MemoryUtilization", "ClusterName", var.ecs_cluster_name, "ServiceName", var.backend_service_name, { stat = "Average" }]
          ]
          period = 60
          yAxis  = { left = { min = 0, max = 100 } }
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 1
        width  = 8
        height = 6
        properties = {
          title   = "Backend Running Tasks"
          region  = var.region
          metrics = [
            ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", var.ecs_cluster_name, "ServiceName", var.backend_service_name, { stat = "Average" }]
          ]
          period = 60
        }
      },

      # ── Row 2: ECS Worker ─────────────────────────────
      {
        type   = "text"
        x      = 0
        y      = 7
        width  = 24
        height = 1
        properties = {
          markdown = "# ECS — Worker"
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 8
        width  = 8
        height = 6
        properties = {
          title   = "Worker CPU Utilization"
          region  = var.region
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", var.ecs_cluster_name, "ServiceName", var.worker_service_name, { stat = "Average" }]
          ]
          period = 60
          yAxis  = { left = { min = 0, max = 100 } }
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 8
        width  = 8
        height = 6
        properties = {
          title   = "Worker Memory Utilization"
          region  = var.region
          metrics = [
            ["AWS/ECS", "MemoryUtilization", "ClusterName", var.ecs_cluster_name, "ServiceName", var.worker_service_name, { stat = "Average" }]
          ]
          period = 60
          yAxis  = { left = { min = 0, max = 100 } }
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 8
        width  = 8
        height = 6
        properties = {
          title   = "Worker Running Tasks"
          region  = var.region
          metrics = [
            ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", var.ecs_cluster_name, "ServiceName", var.worker_service_name, { stat = "Average" }]
          ]
          period = 60
        }
      },

      # ── Row 3: ALB ────────────────────────────────────
      {
        type   = "text"
        x      = 0
        y      = 14
        width  = 24
        height = 1
        properties = {
          markdown = "# Application Load Balancer"
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 15
        width  = 8
        height = 6
        properties = {
          title   = "Request Count"
          region  = var.region
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum" }]
          ]
          period = 60
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 15
        width  = 8
        height = 6
        properties = {
          title   = "Response Time (p50 / p99)"
          region  = var.region
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", var.alb_arn_suffix, { stat = "p50", label = "p50" }],
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", var.alb_arn_suffix, { stat = "p99", label = "p99" }]
          ]
          period = 60
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 15
        width  = 8
        height = 6
        properties = {
          title   = "HTTP Errors (4xx / 5xx)"
          region  = var.region
          metrics = [
            ["AWS/ApplicationELB", "HTTPCode_Target_4XX_Count", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum", label = "4xx" }],
            ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum", label = "5xx" }]
          ]
          period = 60
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 21
        width  = 8
        height = 6
        properties = {
          title   = "Healthy / Unhealthy Hosts"
          region  = var.region
          metrics = [
            ["AWS/ApplicationELB", "HealthyHostCount", "TargetGroup", var.target_group_arn_suffix, "LoadBalancer", var.alb_arn_suffix, { stat = "Average", label = "Healthy" }],
            ["AWS/ApplicationELB", "UnHealthyHostCount", "TargetGroup", var.target_group_arn_suffix, "LoadBalancer", var.alb_arn_suffix, { stat = "Average", label = "Unhealthy" }]
          ]
          period = 60
        }
      },

      # ── Row 4: RDS ────────────────────────────────────
      {
        type   = "text"
        x      = 0
        y      = 27
        width  = 24
        height = 1
        properties = {
          markdown = "# RDS PostgreSQL"
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 28
        width  = 8
        height = 6
        properties = {
          title   = "RDS CPU Utilization"
          region  = var.region
          metrics = [
            ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average" }]
          ]
          period = 60
          yAxis  = { left = { min = 0, max = 100 } }
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 28
        width  = 8
        height = 6
        properties = {
          title   = "Database Connections"
          region  = var.region
          metrics = [
            ["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average" }]
          ]
          period = 60
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 28
        width  = 8
        height = 6
        properties = {
          title   = "Free Storage Space (GB)"
          region  = var.region
          metrics = [
            ["AWS/RDS", "FreeStorageSpace", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average" }]
          ]
          period = 300
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 34
        width  = 8
        height = 6
        properties = {
          title   = "Read / Write IOPS"
          region  = var.region
          metrics = [
            ["AWS/RDS", "ReadIOPS", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average", label = "Read" }],
            ["AWS/RDS", "WriteIOPS", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average", label = "Write" }]
          ]
          period = 60
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 34
        width  = 8
        height = 6
        properties = {
          title   = "Read / Write Latency (ms)"
          region  = var.region
          metrics = [
            ["AWS/RDS", "ReadLatency", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average", label = "Read" }],
            ["AWS/RDS", "WriteLatency", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average", label = "Write" }]
          ]
          period = 60
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 34
        width  = 8
        height = 6
        properties = {
          title   = "Freeable Memory (MB)"
          region  = var.region
          metrics = [
            ["AWS/RDS", "FreeableMemory", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average" }]
          ]
          period = 60
        }
      },

      # ── Row 5: ElastiCache ────────────────────────────
      {
        type   = "text"
        x      = 0
        y      = 40
        width  = 24
        height = 1
        properties = {
          markdown = "# ElastiCache Redis"
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 41
        width  = 8
        height = 6
        properties = {
          title   = "ElastiCache CPU Utilization"
          region  = var.region
          metrics = [
            ["AWS/ElastiCache", "EngineCPUUtilization", "ReplicationGroupId", var.elasticache_replication_group_id, { stat = "Average" }]
          ]
          period = 60
          yAxis  = { left = { min = 0, max = 100 } }
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 41
        width  = 8
        height = 6
        properties = {
          title   = "Memory Usage (%)"
          region  = var.region
          metrics = [
            ["AWS/ElastiCache", "DatabaseMemoryUsagePercentage", "ReplicationGroupId", var.elasticache_replication_group_id, { stat = "Average" }]
          ]
          period = 60
          yAxis  = { left = { min = 0, max = 100 } }
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 41
        width  = 8
        height = 6
        properties = {
          title   = "Cache Hits / Misses"
          region  = var.region
          metrics = [
            ["AWS/ElastiCache", "CacheHits", "ReplicationGroupId", var.elasticache_replication_group_id, { stat = "Sum", label = "Hits" }],
            ["AWS/ElastiCache", "CacheMisses", "ReplicationGroupId", var.elasticache_replication_group_id, { stat = "Sum", label = "Misses" }]
          ]
          period = 60
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 47
        width  = 8
        height = 6
        properties = {
          title   = "Current Connections"
          region  = var.region
          metrics = [
            ["AWS/ElastiCache", "CurrConnections", "ReplicationGroupId", var.elasticache_replication_group_id, { stat = "Average" }]
          ]
          period = 60
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 47
        width  = 8
        height = 6
        properties = {
          title   = "Evictions"
          region  = var.region
          metrics = [
            ["AWS/ElastiCache", "Evictions", "ReplicationGroupId", var.elasticache_replication_group_id, { stat = "Sum" }]
          ]
          period = 60
        }
      }
    ]
  })
}
