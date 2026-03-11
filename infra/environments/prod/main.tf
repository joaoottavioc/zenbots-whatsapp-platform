locals {
  backend_image    = "${var.ecr_repository_url}:${var.image_tag}"
  worker_image     = "${var.ecr_repository_url}:${var.image_tag}-worker"
  migrations_image = "${var.ecr_repository_url}:${var.image_tag}-migrations"
  domain_name      = "${var.domain_prefix}.zenbotz.com.br"
}

# ══════════════════════════ VPC ══════════════════════════

module "vpc" {
  source = "../../modules/vpc"

  project     = var.project
  environment = var.environment
  region      = var.region
}

# ══════════════════════════ NAT ══════════════════════════

module "nat" {
  source = "../../modules/nat"

  project                 = var.project
  environment             = var.environment
  region                  = var.region
  vpc_id                  = module.vpc.vpc_id
  public_subnet_id        = module.vpc.public_subnet_ids[0]
  private_route_table_ids = module.vpc.private_route_table_ids
  nat_type                = var.nat_type # "gateway" for prod ($32/mo)
}

# ══════════════════════════ RDS ══════════════════════════

module "rds" {
  source = "../../modules/rds"

  project           = var.project
  environment       = var.environment
  subnet_ids        = module.vpc.isolated_subnet_ids
  security_group_id = module.vpc.rds_security_group_id
  instance_class    = "db.t4g.small" # Prod: small (vs micro for dev)
  db_password       = var.db_password
  multi_az          = true # HA: automatic failover
  backup_retention_days = 14

  allocated_storage     = 20
  max_allocated_storage = 100
}

# ══════════════════════════ ElastiCache ══════════════════════════

module "elasticache" {
  source = "../../modules/elasticache"

  project            = var.project
  environment        = var.environment
  subnet_ids         = module.vpc.isolated_subnet_ids
  security_group_id  = module.vpc.redis_security_group_id
  node_type          = "cache.t4g.small" # Prod: small (vs micro for dev)
  num_cache_clusters = 2                 # HA: replica in second AZ
}

# ══════════════════════════ S3 ══════════════════════════

module "s3" {
  source = "../../modules/s3"

  project     = var.project
  environment = var.environment
}

# ══════════════════════════ ALB ══════════════════════════

module "alb" {
  source = "../../modules/alb"

  project           = var.project
  environment       = var.environment
  vpc_id            = module.vpc.vpc_id
  public_subnet_ids = module.vpc.public_subnet_ids
  security_group_id = module.vpc.alb_security_group_id
  certificate_arn   = var.certificate_arn
}

# ══════════════════════════ Secrets Manager ══════════════════════════

module "secrets" {
  source = "../../modules/secrets"

  project     = var.project
  environment = var.environment
}

# ══════════════════════════ ECS ══════════════════════════

module "ecs" {
  source = "../../modules/ecs"

  project     = var.project
  environment = var.environment
  region      = var.region
  account_id  = var.account_id

  private_subnet_ids    = module.vpc.private_subnet_ids
  ecs_security_group_id = module.vpc.ecs_security_group_id
  target_group_arn      = module.alb.target_group_arn

  # Prod: On-Demand only (no Spot for production reliability)
  capacity_providers = ["FARGATE"]
  container_insights = true
  cpu_architecture   = "ARM64"

  # Backend — start with 1 task, auto-scale to 4. Bump to 2-task baseline at >20 customers.
  # 1024/2048 required: SentenceTransformer + PyTorch loads ~500MB per worker process
  backend_image         = local.backend_image
  backend_cpu           = 1024 # 1 vCPU (SentenceTransformer needs headroom)
  backend_memory        = 2048 # 2 GB (model ~500MB + app ~200MB + headroom)
  backend_desired_count = 1    # Start lean, auto-scale handles spikes
  backend_max_count     = 4

  backend_environment = [
    { name = "ENVIRONMENT", value = "production" },
    { name = "WEB_WORKERS", value = "1" },
    { name = "LOG_FORMAT", value = "json" },
    { name = "REDIS_HOST", value = module.elasticache.redis_host },
    { name = "REDIS_PORT", value = "6379" },
    { name = "REDIS_URL", value = module.elasticache.redis_url },
    { name = "CORS_ORIGINS", value = "https://app.zenbotz.com.br" },
    { name = "COOKIE_DOMAIN", value = ".zenbotz.com.br" },
    { name = "FRONTEND_URL", value = "https://app.zenbotz.com.br" },
    { name = "MP_REDIRECT_URI", value = "https://app.zenbotz.com.br/pagamentos" },
    { name = "AWS_BUCKET_NAME", value = "zenbots-prod-menus" },
  ]

  backend_secrets = [
    { name = "DATABASE_URL", valueFrom = "${module.secrets.secret_arns["database"]}:url::" },
    { name = "SECRET_KEY", valueFrom = "${module.secrets.secret_arns["auth"]}:secret_key::" },
    { name = "ENCRYPTION_KEY", valueFrom = "${module.secrets.secret_arns["auth"]}:encryption_key::" },
    { name = "OPENAI_API_KEY", valueFrom = "${module.secrets.secret_arns["openai"]}:api_key::" },
    { name = "WHATSAPP_TOKEN", valueFrom = "${module.secrets.secret_arns["whatsapp"]}:token::" },
    { name = "META_VERIFY_TOKEN", valueFrom = "${module.secrets.secret_arns["whatsapp"]}:verify_token::" },
    { name = "FB_APP_ID", valueFrom = "${module.secrets.secret_arns["whatsapp"]}:fb_app_id::" },
    { name = "FB_APP_SECRET", valueFrom = "${module.secrets.secret_arns["whatsapp"]}:fb_app_secret::" },
    { name = "MP_CLIENT_ID", valueFrom = "${module.secrets.secret_arns["mercadopago"]}:client_id::" },
    { name = "MP_CLIENT_SECRET", valueFrom = "${module.secrets.secret_arns["mercadopago"]}:client_secret::" },
    { name = "MP_ADMIN_ACCESS_TOKEN", valueFrom = "${module.secrets.secret_arns["mercadopago"]}:admin_token::" },
    { name = "GOOGLE_MAPS_API_KEY", valueFrom = "${module.secrets.secret_arns["google"]}:maps_api_key::" },
  ]

  # Worker — 1 task + auto-scaling up to 2
  # Worker doesn't load SentenceTransformer (lazy imports), 256/512 is enough
  worker_image         = local.worker_image
  worker_cpu           = 256
  worker_memory        = 512
  worker_desired_count = 1
  worker_max_count     = 2
  worker_stop_timeout  = 120

  worker_environment = [
    { name = "ENVIRONMENT", value = "production" },
    { name = "LOG_FORMAT", value = "json" },
    { name = "REDIS_HOST", value = module.elasticache.redis_host },
    { name = "REDIS_PORT", value = "6379" },
    { name = "REDIS_URL", value = module.elasticache.redis_url },
  ]

  worker_secrets = [
    { name = "DATABASE_URL", valueFrom = "${module.secrets.secret_arns["database"]}:url::" },
    { name = "SECRET_KEY", valueFrom = "${module.secrets.secret_arns["auth"]}:secret_key::" },
    { name = "ENCRYPTION_KEY", valueFrom = "${module.secrets.secret_arns["auth"]}:encryption_key::" },
    { name = "OPENAI_API_KEY", valueFrom = "${module.secrets.secret_arns["openai"]}:api_key::" },
    { name = "WHATSAPP_TOKEN", valueFrom = "${module.secrets.secret_arns["whatsapp"]}:token::" },
    { name = "MP_CLIENT_ID", valueFrom = "${module.secrets.secret_arns["mercadopago"]}:client_id::" },
    { name = "MP_CLIENT_SECRET", valueFrom = "${module.secrets.secret_arns["mercadopago"]}:client_secret::" },
    { name = "MP_ADMIN_ACCESS_TOKEN", valueFrom = "${module.secrets.secret_arns["mercadopago"]}:admin_token::" },
    { name = "GOOGLE_MAPS_API_KEY", valueFrom = "${module.secrets.secret_arns["google"]}:maps_api_key::" },
  ]

  # Migrations
  migrations_image = local.migrations_image

  # Zero-downtime deployment: always keep 100% healthy
  minimum_healthy_percent = 100
  maximum_percent         = 200

  # Logging — 90-day retention for prod
  log_retention_days = 90

  # S3
  s3_bucket_name = module.s3.bucket_name
}

# ══════════════════════════ Monitoring ══════════════════════════

module "monitoring" {
  source = "../../modules/monitoring"

  project              = var.project
  environment          = var.environment
  alert_email          = var.alert_email
  ecs_cluster_name     = module.ecs.cluster_name
  backend_service_name = module.ecs.backend_service_name
  worker_service_name  = module.ecs.worker_service_name
  alb_arn_suffix       = module.alb.alb_arn_suffix
  target_group_arn_suffix = module.alb.target_group_arn_suffix
  rds_instance_id      = "${var.project}-${var.environment}"
  elasticache_replication_group_id = module.elasticache.replication_group_id
}

# ══════════════════════════ Dashboard ══════════════════════════

module "dashboard" {
  source = "../../modules/dashboard"

  project     = var.project
  environment = var.environment
  region      = var.region

  ecs_cluster_name                 = module.ecs.cluster_name
  backend_service_name             = module.ecs.backend_service_name
  worker_service_name              = module.ecs.worker_service_name
  alb_arn_suffix                   = module.alb.alb_arn_suffix
  target_group_arn_suffix          = module.alb.target_group_arn_suffix
  rds_instance_id                  = "${var.project}-${var.environment}"
  elasticache_replication_group_id = module.elasticache.replication_group_id
}

# ══════════════════════════ WAF ══════════════════════════

module "waf" {
  source = "../../modules/waf"

  project     = var.project
  environment = var.environment
  alb_arn     = module.alb.alb_arn
  rate_limit  = 2000 # 2000 requests per 5 minutes per IP
}

# ══════════════════════════ Maintenance Page ══════════════════════════

module "maintenance_page" {
  source = "../../modules/maintenance-page"

  project     = var.project
  environment = var.environment
  domain_name = local.domain_name
}

# ══════════════════════════ Route 53 ══════════════════════════

# Health check on ALB endpoint
resource "aws_route53_health_check" "alb" {
  count = var.route53_zone_id != "" ? 1 : 0

  fqdn              = module.alb.alb_dns_name
  port               = 443
  type               = "HTTPS"
  resource_path      = "/health/live"
  failure_threshold  = 3
  request_interval   = 30

  tags = {
    Name        = "${var.project}-${var.environment}-alb-health"
    Environment = var.environment
    Project     = var.project
  }
}

# Primary DNS record — points to ALB (with health check)
resource "aws_route53_record" "primary" {
  count = var.route53_zone_id != "" ? 1 : 0

  zone_id = var.route53_zone_id
  name    = local.domain_name
  type    = "A"

  alias {
    name                   = module.alb.alb_dns_name
    zone_id                = module.alb.alb_zone_id
    evaluate_target_health = true
  }

  set_identifier = "primary"

  failover_routing_policy {
    type = "PRIMARY"
  }

  health_check_id = aws_route53_health_check.alb[0].id
}

# Failover DNS record — points to S3 maintenance page
resource "aws_route53_record" "failover" {
  count = var.route53_zone_id != "" ? 1 : 0

  zone_id = var.route53_zone_id
  name    = local.domain_name
  type    = "A"

  alias {
    name                   = module.maintenance_page.website_endpoint
    zone_id                = module.maintenance_page.bucket_hosted_zone_id
    evaluate_target_health = false
  }

  set_identifier = "failover"

  failover_routing_policy {
    type = "SECONDARY"
  }
}
