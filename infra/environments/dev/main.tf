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

# ------------------ ElastiCache ------------------

module "elasticache" {
  source = "../../modules/elasticache"

  project            = var.project
  environment        = var.environment
  subnet_ids         = module.vpc.isolated_subnet_ids
  security_group_id  = module.vpc.redis_security_group_id
  node_type          = "cache.t4g.micro"
  num_cache_clusters = 1
}

# ------------------ S3 ------------------

module "s3" {
  source = "../../modules/s3"

  project     = var.project
  environment = var.environment
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

# ------------------ Secrets Manager ------------------

module "secrets" {
  source = "../../modules/secrets"

  project     = var.project
  environment = var.environment
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

  capacity_providers = ["FARGATE_SPOT"]
  container_insights = true
  cpu_architecture   = "ARM64"

  # Backend
  backend_image         = local.backend_image
  backend_cpu           = 256
  backend_memory        = 512
  backend_desired_count = 1
  backend_max_count     = 1

  backend_environment = [
    { name = "ENVIRONMENT", value = "development" },
    { name = "LOG_FORMAT", value = "json" },
    { name = "REDIS_HOST", value = module.elasticache.redis_host },
    { name = "REDIS_PORT", value = "6379" },
    { name = "REDIS_URL", value = module.elasticache.redis_url },
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

  # Deployment
  minimum_healthy_percent = 50
  maximum_percent         = 200

  # Logging
  log_retention_days = 7

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
  elasticache_replication_group_id = module.elasticache.replication_group_id
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
