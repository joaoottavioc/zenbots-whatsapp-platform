# ------------------ NAT Gateway (prod) ------------------

resource "aws_eip" "nat" {
  count  = var.nat_type == "gateway" ? 1 : 0
  domain = "vpc"

  tags = {
    Name = "${var.project}-${var.environment}-nat-eip"
  }
}

resource "aws_nat_gateway" "this" {
  count         = var.nat_type == "gateway" ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = var.public_subnet_id

  tags = {
    Name = "${var.project}-${var.environment}-nat-gw"
  }
}

# ------------------ EIP for Reverse Proxy (stable IP for DNS) ------------------

resource "aws_eip" "nat_instance" {
  count  = var.nat_type == "instance" && var.enable_reverse_proxy ? 1 : 0
  domain = "vpc"

  tags = {
    Name = "${var.project}-${var.environment}-nat-instance-eip"
  }
}

resource "aws_eip_association" "nat_instance" {
  count         = var.nat_type == "instance" && var.enable_reverse_proxy ? 1 : 0
  instance_id   = aws_instance.nat[0].id
  allocation_id = aws_eip.nat_instance[0].id
}

# ------------------ NAT Instance (dev — $3/mo) ------------------

# fck-nat: lightweight, open-source NAT AMI optimized for low-traffic use
# https://fck-nat.dev — ARM64 compatible, much cheaper than managed NAT Gateway
data "aws_ami" "fck_nat" {
  count       = var.nat_type == "instance" ? 1 : 0
  most_recent = true
  owners      = ["568608671756"] # fck-nat community AMI owner

  filter {
    name   = "name"
    values = ["fck-nat-al2023-*-arm64-ebs"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

locals {
  nat_ami_id = var.nat_type == "instance" ? data.aws_ami.fck_nat[0].id : ""
}

resource "aws_security_group" "nat" {
  count       = var.nat_type == "instance" ? 1 : 0
  name_prefix = "${var.project}-${var.environment}-nat-"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = var.private_subnet_cidrs
  }

  # HTTPS ingress for Caddy reverse proxy
  dynamic "ingress" {
    for_each = var.enable_reverse_proxy ? [1] : []
    content {
      from_port   = 443
      to_port     = 443
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
      description = "HTTPS for Caddy reverse proxy"
    }
  }

  # HTTP ingress for Let's Encrypt ACME HTTP-01 challenge
  dynamic "ingress" {
    for_each = var.enable_reverse_proxy ? [1] : []
    content {
      from_port   = 80
      to_port     = 80
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
      description = "HTTP for ACME challenge"
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project}-${var.environment}-nat-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_instance" "nat" {
  count                       = var.nat_type == "instance" ? 1 : 0
  ami                         = local.nat_ami_id
  instance_type               = "t4g.nano"
  subnet_id                   = var.public_subnet_id
  vpc_security_group_ids      = [aws_security_group.nat[0].id]
  source_dest_check           = false
  associate_public_ip_address = true

  user_data = var.enable_reverse_proxy ? templatefile("${path.module}/caddy_userdata.sh.tpl", {
    domain   = var.reverse_proxy_domain
    upstream = var.reverse_proxy_upstream
  }) : null

  user_data_replace_on_change = true

  tags = {
    Name = "${var.project}-${var.environment}-nat-instance"
  }

  lifecycle {
    # fck-nat publishes new AMI builds periodically; `most_recent = true`
    # above means any unrelated apply would otherwise force-replace this
    # instance (and its EIP association) to pick up the new AMI, causing a
    # brief egress outage for every ECS task. AMI upgrades should be a
    # deliberate `terraform apply -replace=module.nat.aws_instance.nat[0]`,
    # not a side effect.
    ignore_changes = [ami]
  }
}

# Auto-recovery: restart instance if status checks fail
resource "aws_cloudwatch_metric_alarm" "nat_recovery" {
  count               = var.nat_type == "instance" ? 1 : 0
  alarm_name          = "${var.project}-${var.environment}-nat-recovery"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_System"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0

  dimensions = {
    InstanceId = aws_instance.nat[0].id
  }

  alarm_actions = ["arn:aws:automate:${var.region}:ec2:recover"]
}

# ------------------ Routes ------------------

resource "aws_route" "private_nat" {
  count                  = length(var.private_route_table_ids)
  route_table_id         = var.private_route_table_ids[count.index]
  destination_cidr_block = "0.0.0.0/0"

  nat_gateway_id       = var.nat_type == "gateway" ? aws_nat_gateway.this[0].id : null
  network_interface_id = var.nat_type == "instance" ? aws_instance.nat[0].primary_network_interface_id : null
}
