locals {
  backend_image    = "${var.ecr_repository_url}:${var.image_tag}"
  worker_image     = "${var.ecr_repository_url}:${var.image_tag}-worker"
  migrations_image = "${var.ecr_repository_url}:${var.image_tag}-migrations"
}

# ------------------ VPC ------------------

module "vpc" {
  source = "../../modules/vpc"

  project     = var.project
  environment = var.environment
  region      = var.region
}

# ------------------ NAT ------------------

module "nat" {
  source = "../../modules/nat"

  project                 = var.project
  environment             = var.environment
  region                  = var.region
  vpc_id                  = module.vpc.vpc_id
  public_subnet_id        = module.vpc.public_subnet_ids[0]
  private_route_table_ids = module.vpc.private_route_table_ids
  nat_type                = var.nat_type
}

# ------------------ RDS ------------------

module "rds" {
  source = "../../modules/rds"

  project           = var.project
  environment       = var.environment
  subnet_ids        = module.vpc.isolated_subnet_ids
  security_group_id = module.vpc.rds_security_group_id
  instance_class    = "db.t4g.micro"
  db_password       = var.db_password
  multi_az          = false
}

# ------------------ Redis on ECS (replaces ElastiCache for dev) --

module "redis_ecs" {
  source = "../../modules/redis-ecs"

  project     = var.project
  environment = var.environment
  region      = var.region

  vpc_id                = module.vpc.vpc_id
  private_subnet_ids    = module.vpc.private_subnet_ids
  ecs_security_group_id = module.vpc.ecs_security_group_id

  cluster_name       = module.ecs.cluster_name
  capacity_providers = ["FARGATE_SPOT"]
  execution_role_arn = module.ecs.execution_role_arn
  cpu_architecture   = "ARM64"

  cpu           = 256
  memory        = 512
  maxmemory     = 384
  desired_count = 1

  log_retention_days = 3
}

# ------------------ S3 ------------------

module "s3" {
  source = "../../modules/s3"

  project            = var.project
  environment        = var.environment
  versioning_enabled = false
}

# ------------------ ALB ------------------

module "alb" {
  source = "../../modules/alb"

  project           = var.project
  environment       = var.environment
  vpc_id            = module.vpc.vpc_id
  public_subnet_ids = module.vpc.public_subnet_ids
  security_group_id = module.vpc.alb_security_group_id
  certificate_arn   = var.certificate_arn
}

# ------------------ Secrets Manager (kept for cleanup later) ------------------

module "secrets" {
  source = "../../modules/secrets"

  project     = var.project
  environment = var.environment
}

# ------------------ SSM Parameter Store (replaces Secrets Manager for dev) --

module "ssm_parameters" {
  source = "../../modules/ssm-parameters"

  project     = var.project
  environment = var.environment

  parameters = {
    "database/url"              = "PostgreSQL connection URL"
    "auth/secret_key"           = "JWT signing secret"
    "auth/encryption_key"       = "Fernet encryption key for payment tokens"
    "openai/api_key"            = "OpenAI API key"
    "whatsapp/token"            = "WhatsApp Cloud API token"
    "whatsapp/verify_token"     = "WhatsApp webhook verify token"
    "whatsapp/fb_app_id"        = "Facebook App ID for Embedded Signup"
    "whatsapp/fb_app_secret"    = "Facebook App Secret for Embedded Signup"
    "mercadopago/client_id"     = "Mercado Pago OAuth client ID"
    "mercadopago/client_secret" = "Mercado Pago OAuth client secret"
    "mercadopago/admin_token"   = "Mercado Pago admin access token"
    "google/maps_api_key"       = "Google Maps API key for CEP lookup"
    "email/server"              = "SMTP server hostname"
    "email/port"                = "SMTP server port"
    "email/username"            = "SMTP username"
    "email/password"            = "SMTP password"
    "email/from"                = "Email sender address"
    "email/starttls"            = "SMTP STARTTLS flag"
    "email/use_credentials"     = "SMTP use credentials flag"
  }
}

# ------------------ ECS ------------------

module "ecs" {
  source = "../../modules/ecs"

  project     = var.project
  environment = var.environment
  region      = var.region
  account_id  = var.account_id

  private_subnet_ids    = module.vpc.private_subnet_ids
  ecs_security_group_id = module.vpc.ecs_security_group_id
  target_group_arn      = module.alb.target_group_arn

  capacity_providers  = ["FARGATE_SPOT"]
  container_insights  = false
  cpu_architecture    = "ARM64"
  use_ssm_parameters  = true

  # Backend
  backend_image         = local.backend_image
  backend_cpu           = 1024
  backend_memory        = 2048
  backend_desired_count = 1
  backend_max_count     = 1

  backend_environment = [
    { name = "ENVIRONMENT", value = "development" },
    { name = "WEB_WORKERS", value = "1" },
    { name = "LOG_FORMAT", value = "json" },
    { name = "REDIS_HOST", value = module.redis_ecs.redis_host },
    { name = "REDIS_PORT", value = "6379" },
    { name = "REDIS_URL", value = module.redis_ecs.redis_url },
    { name = "CORS_ORIGINS", value = "https://dev.zenbotz.com.br" },
    { name = "COOKIE_DOMAIN", value = ".zenbotz.com.br" },
    { name = "FRONTEND_URL", value = "https://dev.zenbotz.com.br" },
    { name = "AWS_BUCKET_NAME", value = "zenbots-dev-menus" },
  ]

  backend_secrets = [
    { name = "DATABASE_URL", valueFrom = module.ssm_parameters.parameter_arns["database/url"] },
    { name = "SECRET_KEY", valueFrom = module.ssm_parameters.parameter_arns["auth/secret_key"] },
    { name = "ENCRYPTION_KEY", valueFrom = module.ssm_parameters.parameter_arns["auth/encryption_key"] },
    { name = "OPENAI_API_KEY", valueFrom = module.ssm_parameters.parameter_arns["openai/api_key"] },
    { name = "WHATSAPP_TOKEN", valueFrom = module.ssm_parameters.parameter_arns["whatsapp/token"] },
    { name = "META_VERIFY_TOKEN", valueFrom = module.ssm_parameters.parameter_arns["whatsapp/verify_token"] },
    { name = "FB_APP_ID", valueFrom = module.ssm_parameters.parameter_arns["whatsapp/fb_app_id"] },
    { name = "FB_APP_SECRET", valueFrom = module.ssm_parameters.parameter_arns["whatsapp/fb_app_secret"] },
    { name = "MP_CLIENT_ID", valueFrom = module.ssm_parameters.parameter_arns["mercadopago/client_id"] },
    { name = "MP_CLIENT_SECRET", valueFrom = module.ssm_parameters.parameter_arns["mercadopago/client_secret"] },
    { name = "MP_ADMIN_ACCESS_TOKEN", valueFrom = module.ssm_parameters.parameter_arns["mercadopago/admin_token"] },
    { name = "GOOGLE_MAPS_API_KEY", valueFrom = module.ssm_parameters.parameter_arns["google/maps_api_key"] },
    { name = "MAIL_SERVER", valueFrom = module.ssm_parameters.parameter_arns["email/server"] },
    { name = "MAIL_PORT", valueFrom = module.ssm_parameters.parameter_arns["email/port"] },
    { name = "MAIL_USERNAME", valueFrom = module.ssm_parameters.parameter_arns["email/username"] },
    { name = "MAIL_PASSWORD", valueFrom = module.ssm_parameters.parameter_arns["email/password"] },
    { name = "MAIL_FROM", valueFrom = module.ssm_parameters.parameter_arns["email/from"] },
    { name = "MAIL_STARTTLS", valueFrom = module.ssm_parameters.parameter_arns["email/starttls"] },
    { name = "USE_CREDENTIALS", valueFrom = module.ssm_parameters.parameter_arns["email/use_credentials"] },
  ]

  # Worker
  worker_image         = local.worker_image
  worker_cpu           = 256
  worker_memory        = 512
  worker_desired_count = 1
  worker_max_count     = 1
  worker_stop_timeout  = 120

  worker_environment = [
    { name = "ENVIRONMENT", value = "development" },
    { name = "LOG_FORMAT", value = "json" },
    { name = "REDIS_HOST", value = module.redis_ecs.redis_host },
    { name = "REDIS_PORT", value = "6379" },
    { name = "REDIS_URL", value = module.redis_ecs.redis_url },
  ]

  worker_secrets = [
    { name = "DATABASE_URL", valueFrom = module.ssm_parameters.parameter_arns["database/url"] },
    { name = "SECRET_KEY", valueFrom = module.ssm_parameters.parameter_arns["auth/secret_key"] },
    { name = "ENCRYPTION_KEY", valueFrom = module.ssm_parameters.parameter_arns["auth/encryption_key"] },
    { name = "OPENAI_API_KEY", valueFrom = module.ssm_parameters.parameter_arns["openai/api_key"] },
    { name = "WHATSAPP_TOKEN", valueFrom = module.ssm_parameters.parameter_arns["whatsapp/token"] },
    { name = "MP_CLIENT_ID", valueFrom = module.ssm_parameters.parameter_arns["mercadopago/client_id"] },
    { name = "MP_CLIENT_SECRET", valueFrom = module.ssm_parameters.parameter_arns["mercadopago/client_secret"] },
    { name = "MP_ADMIN_ACCESS_TOKEN", valueFrom = module.ssm_parameters.parameter_arns["mercadopago/admin_token"] },
    { name = "GOOGLE_MAPS_API_KEY", valueFrom = module.ssm_parameters.parameter_arns["google/maps_api_key"] },
  ]

  # Migrations
  migrations_image = local.migrations_image

  # Deployment
  minimum_healthy_percent = 50
  maximum_percent         = 200

  # Logging
  log_retention_days = 3

  # S3
  s3_bucket_name = module.s3.bucket_name
}

# ------------------ Monitoring ------------------

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
  elasticache_replication_group_id = "" # Dev uses Redis on ECS, no ElastiCache alarms
}

# ------------------ Scheduling (off-hours) ------------------

module "scheduling" {
  source = "../../modules/scheduling"

  project              = var.project
  environment          = var.environment
  region               = var.region
  account_id           = var.account_id
  enable_scheduling    = true
  ecs_cluster_name     = module.ecs.cluster_name
  backend_service_name = module.ecs.backend_service_name
  worker_service_name  = module.ecs.worker_service_name
  backend_desired_count = 1
  worker_desired_count  = 1

  # Redis ECS scheduling (starts before backend/worker, stops after)
  redis_service_name  = module.redis_ecs.service_name
  redis_desired_count = 1
}

# ------------------ RDS Scheduling (off-hours stop/start) --

module "rds_scheduling" {
  source = "../../modules/rds-scheduling"

  project           = var.project
  environment       = var.environment
  region            = var.region
  account_id        = var.account_id
  enable_scheduling = true
  rds_instance_id   = "${var.project}-${var.environment}"
}

# ------------------ Route 53 (optional) ------------------

resource "aws_route53_record" "dev_api" {
  count   = var.route53_zone_id != "" ? 1 : 0
  zone_id = var.route53_zone_id
  name    = "${var.domain_prefix}.zenbotz.com.br"
  type    = "A"

  alias {
    name                   = module.alb.alb_dns_name
    zone_id                = module.alb.alb_zone_id
    evaluate_target_health = true
  }
}
