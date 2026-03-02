resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.project}-${var.environment}-redis"
  subnet_ids = var.subnet_ids

  tags = {
    Name = "${var.project}-${var.environment}-redis-subnet-group"
  }
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = "${var.project}-${var.environment}"
  description          = "${var.project} ${var.environment} Redis"

  node_type            = var.node_type
  num_cache_clusters   = var.num_cache_clusters
  port                 = 6379
  parameter_group_name = "default.redis7"
  engine_version       = "7.0"

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [var.security_group_id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = false # Simplifies connection strings; enable if needed

  automatic_failover_enabled = var.num_cache_clusters > 1 ? true : false
  multi_az_enabled           = var.num_cache_clusters > 1 ? true : false

  snapshot_retention_limit = var.environment == "prod" ? 7 : 0

  tags = {
    Name = "${var.project}-${var.environment}-redis"
  }
}
