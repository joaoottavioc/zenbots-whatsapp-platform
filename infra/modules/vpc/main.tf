data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

# ------------------ VPC ------------------

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.project}-${var.environment}-vpc"
  }
}

# ------------------ Internet Gateway ------------------

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${var.project}-${var.environment}-igw"
  }
}

# ------------------ Public Subnets (ALB, NAT) ------------------

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.project}-${var.environment}-public-${local.azs[count.index]}"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = {
    Name = "${var.project}-${var.environment}-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ------------------ Private Subnets (ECS tasks) ------------------

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = {
    Name = "${var.project}-${var.environment}-private-${local.azs[count.index]}"
  }
}

resource "aws_route_table" "private" {
  count  = 2
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${var.project}-${var.environment}-private-rt-${local.azs[count.index]}"
  }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# ------------------ Isolated Subnets (RDS, ElastiCache) ------------------

resource "aws_subnet" "isolated" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.isolated_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = {
    Name = "${var.project}-${var.environment}-isolated-${local.azs[count.index]}"
  }
}

resource "aws_route_table" "isolated" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${var.project}-${var.environment}-isolated-rt"
  }
}

resource "aws_route_table_association" "isolated" {
  count          = 2
  subnet_id      = aws_subnet.isolated[count.index].id
  route_table_id = aws_route_table.isolated.id
}

# ------------------ S3 Gateway Endpoint (free, bypasses NAT) ------------------

resource "aws_vpc_endpoint" "s3" {
  vpc_id       = aws_vpc.this.id
  service_name = "com.amazonaws.${var.region}.s3"

  route_table_ids = concat(
    aws_route_table.private[*].id,
    [aws_route_table.isolated.id],
  )

  tags = {
    Name = "${var.project}-${var.environment}-s3-endpoint"
  }
}

# ------------------ Security Groups ------------------

resource "aws_security_group" "alb" {
  name_prefix = "${var.project}-${var.environment}-alb-"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project}-${var.environment}-alb-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "ecs" {
  name_prefix = "${var.project}-${var.environment}-ecs-"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project}-${var.environment}-ecs-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "rds" {
  name_prefix = "${var.project}-${var.environment}-rds-"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }

  tags = {
    Name = "${var.project}-${var.environment}-rds-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "redis" {
  name_prefix = "${var.project}-${var.environment}-redis-"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }

  tags = {
    Name = "${var.project}-${var.environment}-redis-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}
