resource "aws_db_subnet_group" "this" {
  name       = "${var.project}-${var.environment}-db"
  subnet_ids = var.subnet_ids

  tags = {
    Name = "${var.project}-${var.environment}-db-subnet-group"
  }
}

resource "aws_db_parameter_group" "this" {
  name_prefix = "${var.project}-${var.environment}-pg14-"
  family      = "postgres14"

  parameter {
    name  = "shared_preload_libraries"
    value = "vector"
  }

  tags = {
    Name = "${var.project}-${var.environment}-pg-params"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "this" {
  identifier     = "${var.project}-${var.environment}"
  engine         = "postgres"
  engine_version = "14"

  instance_class        = var.instance_class
  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.security_group_id]
  parameter_group_name   = aws_db_parameter_group.this.name

  multi_az            = var.multi_az
  publicly_accessible = false
  skip_final_snapshot = var.environment == "dev" ? true : false
  final_snapshot_identifier = var.environment == "dev" ? null : "${var.project}-${var.environment}-final"

  backup_retention_period = var.backup_retention_days
  backup_window           = "03:00-04:00"
  maintenance_window      = "mon:04:00-mon:05:00"

  deletion_protection = var.environment == "prod" ? true : false

  tags = {
    Name = "${var.project}-${var.environment}-rds"
  }
}
