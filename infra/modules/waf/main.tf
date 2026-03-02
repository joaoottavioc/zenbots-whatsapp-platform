# AWS WAF Web ACL for production ALB

resource "aws_wafv2_web_acl" "this" {
  name  = "${var.project}-${var.environment}-waf"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  # Rate limiting per IP
  rule {
    name     = "rate-limit"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.rate_limit
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project}-${var.environment}-rate-limit"
    }
  }

  # AWS Managed Rules — Common Rule Set (blocks known bad inputs)
  rule {
    name     = "aws-managed-common"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"

        # Exclude rules that might block legitimate API requests
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }

        rule_action_override {
          name = "NoUserAgent_HEADER"
          action_to_use {
            count {}
          }
        }
      }
    }

    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project}-${var.environment}-common-rules"
    }
  }

  # AWS Managed Rules — Known Bad Inputs (SQLi, XSS, etc.)
  rule {
    name     = "aws-managed-bad-inputs"
    priority = 3

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project}-${var.environment}-bad-inputs"
    }
  }

  visibility_config {
    sampled_requests_enabled   = true
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.project}-${var.environment}-waf"
  }

  tags = {
    Name        = "${var.project}-${var.environment}-waf"
    Environment = var.environment
    Project     = var.project
  }
}

# Associate WAF with ALB
resource "aws_wafv2_web_acl_association" "this" {
  resource_arn = var.alb_arn
  web_acl_arn  = aws_wafv2_web_acl.this.arn
}
