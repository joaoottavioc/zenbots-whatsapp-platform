"""Tests for Phase 3: Replace ElastiCache with Redis on ECS (dev only).

Validates the Terraform configuration files to ensure:
1. The redis-ecs module is correctly structured
2. Dev environment wiring replaces ElastiCache with Redis ECS
3. Prod environment is NOT impacted (still uses ElastiCache)
4. Monitoring alarms are conditional on ElastiCache presence
5. Scheduling includes Redis ECS service
6. Security group rules allow ECS-to-ECS Redis communication
7. Cloud Map service discovery is configured
"""

import re
from pathlib import Path

import pytest

INFRA_DIR = Path(__file__).parent.parent / "infra"
MODULES_DIR = INFRA_DIR / "modules"
DEV_DIR = INFRA_DIR / "environments" / "dev"
PROD_DIR = INFRA_DIR / "environments" / "prod"


def read_tf(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_all_tf_in(directory: Path) -> str:
    """Read and concatenate all .tf files in a directory."""
    content = ""
    for tf_file in sorted(directory.glob("*.tf")):
        content += tf_file.read_text(encoding="utf-8") + "\n"
    return content


# ============================================================
# Module Structure Tests
# ============================================================


class TestRedisEcsModuleStructure:
    """Verify the redis-ecs module has all required files and resources."""

    module_dir = MODULES_DIR / "redis-ecs"

    def test_module_directory_exists(self):
        assert self.module_dir.is_dir(), "infra/modules/redis-ecs/ must exist"

    def test_main_tf_exists(self):
        assert (self.module_dir / "main.tf").is_file()

    def test_variables_tf_exists(self):
        assert (self.module_dir / "variables.tf").is_file()

    def test_outputs_tf_exists(self):
        assert (self.module_dir / "outputs.tf").is_file()

    def test_has_cloud_map_namespace(self):
        content = read_tf(self.module_dir / "main.tf")
        assert "aws_service_discovery_private_dns_namespace" in content

    def test_has_cloud_map_service(self):
        content = read_tf(self.module_dir / "main.tf")
        assert "aws_service_discovery_service" in content

    def test_has_ecs_task_definition(self):
        content = read_tf(self.module_dir / "main.tf")
        assert "aws_ecs_task_definition" in content
        assert '"redis"' in content

    def test_has_ecs_service(self):
        content = read_tf(self.module_dir / "main.tf")
        assert "aws_ecs_service" in content

    def test_has_security_group_rule(self):
        content = read_tf(self.module_dir / "main.tf")
        assert "aws_security_group_rule" in content
        assert "6379" in content

    def test_has_log_group(self):
        content = read_tf(self.module_dir / "main.tf")
        assert "aws_cloudwatch_log_group" in content


# ============================================================
# Redis Container Configuration Tests
# ============================================================


class TestRedisContainerConfig:
    """Verify the Redis container is configured correctly."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = read_tf(MODULES_DIR / "redis-ecs" / "main.tf")

    def test_uses_redis_alpine_image(self):
        assert "redis:7-alpine" in self.content

    def test_no_persistence(self):
        """Redis should have no persistence for dev (ephemeral data only)."""
        assert '"--save", ""' in self.content
        assert '"--appendonly", "no"' in self.content

    def test_has_maxmemory_config(self):
        assert "--maxmemory" in self.content
        assert "--maxmemory-policy" in self.content

    def test_has_health_check(self):
        assert "redis-cli ping" in self.content
        assert "PONG" in self.content

    def test_port_6379(self):
        assert "6379" in self.content

    def test_uses_fargate(self):
        assert '"FARGATE"' in self.content

    def test_uses_awsvpc_network_mode(self):
        assert '"awsvpc"' in self.content

    def test_has_service_registries(self):
        """ECS service must register with Cloud Map for DNS discovery."""
        assert "service_registries" in self.content

    def test_has_circuit_breaker(self):
        assert "deployment_circuit_breaker" in self.content


# ============================================================
# Cloud Map Service Discovery Tests
# ============================================================


class TestCloudMapConfig:
    """Verify Cloud Map is set up for DNS-based Redis discovery."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.main = read_tf(MODULES_DIR / "redis-ecs" / "main.tf")
        self.outputs = read_tf(MODULES_DIR / "redis-ecs" / "outputs.tf")

    def test_namespace_uses_local_domain(self):
        """Namespace should be {project}-{env}.local"""
        assert ".local" in self.main

    def test_dns_record_type_a(self):
        assert '"A"' in self.main

    def test_dns_ttl_short(self):
        """TTL should be short (10s) for fast failover."""
        assert "ttl  = 10" in self.main or "ttl = 10" in self.main

    def test_output_redis_host_is_cloud_map_dns(self):
        assert "redis." in self.outputs
        # The .local suffix comes from the namespace name defined in main.tf
        assert "aws_service_discovery_private_dns_namespace" in self.outputs

    def test_output_redis_url_includes_db0(self):
        """Default URL should point to DB 0 (rate limiter)."""
        assert "/0" in self.outputs

    def test_output_service_name(self):
        """Service name output needed by scheduling module."""
        assert "service_name" in self.outputs

    def test_output_namespace_id(self):
        """Namespace ID output needed for Phase 4."""
        assert "namespace_id" in self.outputs


# ============================================================
# Security Group Tests
# ============================================================


class TestSecurityGroupRules:
    """Verify ECS-to-ECS communication is allowed on port 6379."""

    def test_self_referencing_rule_exists(self):
        content = read_tf(MODULES_DIR / "redis-ecs" / "main.tf")
        # Should have source_security_group_id = ecs_security_group_id (self-ref)
        assert "source_security_group_id" in content
        assert "var.ecs_security_group_id" in content

    def test_rule_allows_port_6379(self):
        content = read_tf(MODULES_DIR / "redis-ecs" / "main.tf")
        assert "from_port" in content
        assert "6379" in content

    def test_rule_is_ingress(self):
        content = read_tf(MODULES_DIR / "redis-ecs" / "main.tf")
        # Find the security group rule block
        assert '"ingress"' in content

    def test_vpc_redis_sg_still_exists(self):
        """The VPC's Redis SG should still exist (used by prod ElastiCache)."""
        vpc_content = read_tf(MODULES_DIR / "vpc" / "main.tf")
        assert "aws_security_group" in vpc_content
        assert "redis" in vpc_content


# ============================================================
# Dev Environment Wiring Tests
# ============================================================


class TestDevEnvironment:
    """Verify dev environment uses Redis ECS instead of ElastiCache."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = read_tf(DEV_DIR / "main.tf")

    def test_no_elasticache_module(self):
        """Dev should NOT reference the elasticache module."""
        # Check there's no 'module "elasticache"' block
        assert 'module "elasticache"' not in self.content

    def test_has_redis_ecs_module(self):
        assert 'module "redis_ecs"' in self.content

    def test_redis_ecs_uses_correct_source(self):
        assert "../../modules/redis-ecs" in self.content

    def test_redis_host_from_redis_ecs(self):
        assert "module.redis_ecs.redis_host" in self.content

    def test_redis_url_from_redis_ecs(self):
        assert "module.redis_ecs.redis_url" in self.content

    def test_no_elasticache_redis_host(self):
        """No references to module.elasticache should remain."""
        assert "module.elasticache" not in self.content

    def test_monitoring_no_elasticache_id(self):
        """Monitoring should get empty string for elasticache_replication_group_id."""
        # Find the monitoring block and check the value
        assert 'elasticache_replication_group_id = ""' in self.content

    def test_scheduling_has_redis_service(self):
        assert "redis_service_name" in self.content
        assert "module.redis_ecs.service_name" in self.content

    def test_uses_fargate_spot(self):
        """Redis ECS should use FARGATE_SPOT for cost savings."""
        # Find the redis_ecs module block
        redis_block_match = re.search(
            r'module "redis_ecs".*?^}', self.content, re.DOTALL | re.MULTILINE
        )
        assert redis_block_match, "redis_ecs module block not found"
        redis_block = redis_block_match.group()
        assert "FARGATE_SPOT" in redis_block

    def test_uses_arm64(self):
        redis_block_match = re.search(
            r'module "redis_ecs".*?^}', self.content, re.DOTALL | re.MULTILINE
        )
        redis_block = redis_block_match.group()
        assert "ARM64" in redis_block

    def test_log_retention_3_days(self):
        redis_block_match = re.search(
            r'module "redis_ecs".*?^}', self.content, re.DOTALL | re.MULTILINE
        )
        redis_block = redis_block_match.group()
        assert "log_retention_days = 3" in redis_block

    def test_redis_ecs_connected_to_private_subnets(self):
        """Redis ECS task runs in private subnets (same as backend/worker)."""
        redis_block_match = re.search(
            r'module "redis_ecs".*?^}', self.content, re.DOTALL | re.MULTILINE
        )
        redis_block = redis_block_match.group()
        assert "private_subnet_ids" in redis_block

    def test_redis_ecs_uses_ecs_cluster(self):
        redis_block_match = re.search(
            r'module "redis_ecs".*?^}', self.content, re.DOTALL | re.MULTILINE
        )
        redis_block = redis_block_match.group()
        assert "module.ecs.cluster_name" in redis_block


# ============================================================
# Dev Outputs Tests
# ============================================================


class TestDevOutputs:
    """Verify dev outputs reference Redis ECS instead of ElastiCache."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = read_tf(DEV_DIR / "outputs.tf")

    def test_redis_endpoint_output_exists(self):
        assert "redis_endpoint" in self.content

    def test_redis_endpoint_uses_redis_ecs(self):
        assert "module.redis_ecs" in self.content

    def test_no_elasticache_reference(self):
        assert "module.elasticache" not in self.content


# ============================================================
# Prod Environment Isolation Tests
# ============================================================


class TestProdUnchanged:
    """Verify prod environment is NOT impacted by Phase 3 changes."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = read_tf(PROD_DIR / "main.tf")

    def test_prod_still_uses_elasticache(self):
        assert 'module "elasticache"' in self.content

    def test_prod_no_redis_ecs(self):
        assert 'module "redis_ecs"' not in self.content

    def test_prod_elasticache_multi_az(self):
        """Prod should have 2 cache clusters for HA."""
        assert "num_cache_clusters = 2" in self.content

    def test_prod_monitoring_has_elasticache_id(self):
        assert "module.elasticache.replication_group_id" in self.content

    def test_prod_redis_host_from_elasticache(self):
        assert "module.elasticache.redis_host" in self.content

    def test_prod_redis_url_from_elasticache(self):
        assert "module.elasticache.redis_url" in self.content


# ============================================================
# Monitoring Conditional Alarms Tests
# ============================================================


class TestMonitoringConditionalAlarms:
    """Verify ElastiCache alarms are conditional."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = read_tf(MODULES_DIR / "monitoring" / "main.tf")
        self.variables = read_tf(MODULES_DIR / "monitoring" / "variables.tf")

    def test_elasticache_variable_has_default_empty(self):
        assert 'default     = ""' in self.variables or 'default = ""' in self.variables

    def test_redis_cpu_alarm_is_conditional(self):
        # Find the redis_cpu_high alarm and check it has count
        match = re.search(
            r'resource "aws_cloudwatch_metric_alarm" "redis_cpu_high".*?\n}',
            self.content,
            re.DOTALL,
        )
        assert match, "redis_cpu_high alarm not found"
        alarm_block = match.group()
        assert "count" in alarm_block
        assert 'var.elasticache_replication_group_id != ""' in alarm_block

    def test_redis_memory_alarm_is_conditional(self):
        match = re.search(
            r'resource "aws_cloudwatch_metric_alarm" "redis_memory_high".*?\n}',
            self.content,
            re.DOTALL,
        )
        assert match, "redis_memory_high alarm not found"
        alarm_block = match.group()
        assert "count" in alarm_block

    def test_redis_evictions_alarm_is_conditional(self):
        match = re.search(
            r'resource "aws_cloudwatch_metric_alarm" "redis_evictions".*?\n}',
            self.content,
            re.DOTALL,
        )
        assert match, "redis_evictions alarm not found"
        alarm_block = match.group()
        assert "count" in alarm_block

    def test_ecs_alarms_not_affected(self):
        """ECS alarms should NOT have count conditionals."""
        match = re.search(
            r'resource "aws_cloudwatch_metric_alarm" "backend_cpu_high".*?\n}',
            self.content,
            re.DOTALL,
        )
        assert match
        alarm_block = match.group()
        assert "count" not in alarm_block

    def test_rds_alarms_not_affected(self):
        """RDS alarms should NOT have count conditionals."""
        match = re.search(
            r'resource "aws_cloudwatch_metric_alarm" "rds_cpu_high".*?\n}',
            self.content,
            re.DOTALL,
        )
        assert match
        alarm_block = match.group()
        assert "count" not in alarm_block


# ============================================================
# Scheduling Tests
# ============================================================


class TestSchedulingRedisSupport:
    """Verify the scheduling module supports Redis ECS service."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.main = read_tf(MODULES_DIR / "scheduling" / "main.tf")
        self.variables = read_tf(MODULES_DIR / "scheduling" / "variables.tf")

    def test_redis_service_name_variable_exists(self):
        assert "redis_service_name" in self.variables

    def test_redis_service_name_defaults_empty(self):
        assert 'default     = ""' in self.variables or 'default = ""' in self.variables

    def test_redis_desired_count_variable_exists(self):
        assert "redis_desired_count" in self.variables

    def test_has_redis_scaling_target(self):
        assert "aws_appautoscaling_target" in self.main
        assert '"redis"' in self.main or "redis" in self.main

    def test_redis_scale_down_action_exists(self):
        assert "scale_down_redis" in self.main

    def test_redis_scale_up_action_exists(self):
        assert "scale_up_redis" in self.main

    def test_redis_weekend_scale_down_exists(self):
        assert "scale_down_weekend_redis" in self.main

    def test_redis_scheduling_is_conditional(self):
        """Redis scheduling should only activate when redis_service_name is set."""
        assert "enable_redis_scheduling" in self.main
        assert 'var.redis_service_name != ""' in self.main

    def test_redis_scales_up_before_backend(self):
        """Redis should scale up BEFORE backend/worker (earlier cron time).

        Backend/worker scale up at 12:00 PM UTC.
        Redis should scale up at 11:45 AM UTC (15 min earlier).
        """
        # Find Redis scale up schedule
        redis_up_match = re.search(
            r'resource "aws_appautoscaling_scheduled_action" "scale_up_redis".*?\n}',
            self.main,
            re.DOTALL,
        )
        assert redis_up_match
        redis_up = redis_up_match.group()

        # Backend scales up at "cron(0 12 ...)"
        # Redis should scale up earlier
        assert "11" in redis_up, (
            "Redis should scale up before noon UTC (before backend)"
        )

    def test_redis_scales_down_after_backend(self):
        """Redis should scale down AFTER backend/worker (later cron time).

        Backend/worker scale down at 10:00 PM UTC.
        Redis should scale down at 10:15 PM UTC (15 min later).
        """
        redis_down_match = re.search(
            r'resource "aws_appautoscaling_scheduled_action" "scale_down_redis".*?\n}',
            self.main,
            re.DOTALL,
        )
        assert redis_down_match
        redis_down = redis_down_match.group()

        # Backend scales down at "cron(0 22 ...)"
        # Redis should scale down later (e.g. cron(15 22 ...))
        assert "15 22" in redis_down, "Redis should scale down 15 min after backend"

    def test_existing_backend_scheduling_unchanged(self):
        """Backend/worker scheduling should not be modified."""
        assert "scale_down_backend" in self.main
        assert "scale_up_backend" in self.main
        assert "scale_down_worker" in self.main
        assert "scale_up_worker" in self.main


# ============================================================
# Module Variables Completeness Tests
# ============================================================


class TestRedisEcsVariables:
    """Verify the redis-ecs module has all required variables."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = read_tf(MODULES_DIR / "redis-ecs" / "variables.tf")

    @pytest.mark.parametrize(
        "var_name",
        [
            "project",
            "environment",
            "region",
            "vpc_id",
            "private_subnet_ids",
            "ecs_security_group_id",
            "cluster_name",
            "capacity_providers",
            "execution_role_arn",
            "cpu",
            "memory",
            "cpu_architecture",
            "maxmemory",
            "desired_count",
            "log_retention_days",
        ],
    )
    def test_variable_exists(self, var_name):
        assert f'"{var_name}"' in self.content


# ============================================================
# Consistency Tests
# ============================================================


class TestConsistency:
    """Cross-module consistency checks."""

    def test_dev_env_vars_match_across_backend_and_worker(self):
        """Backend and worker should get the same REDIS_HOST and REDIS_URL."""
        content = read_tf(DEV_DIR / "main.tf")

        # Count redis_ecs references for REDIS_HOST
        redis_host_refs = content.count("module.redis_ecs.redis_host")
        assert redis_host_refs == 2, (
            f"Expected 2 REDIS_HOST refs (backend + worker), got {redis_host_refs}"
        )

        redis_url_refs = content.count("module.redis_ecs.redis_url")
        assert redis_url_refs == 2, (
            f"Expected 2 REDIS_URL refs (backend + worker), got {redis_url_refs}"
        )

    def test_elasticache_module_not_deleted(self):
        """The elasticache module should still exist (used by prod)."""
        assert (MODULES_DIR / "elasticache").is_dir()
        assert (MODULES_DIR / "elasticache" / "main.tf").is_file()

    def test_prod_and_dev_have_different_redis_sources(self):
        """Dev uses redis-ecs, prod uses elasticache."""
        dev = read_tf(DEV_DIR / "main.tf")
        prod = read_tf(PROD_DIR / "main.tf")

        assert "redis-ecs" in dev
        assert "redis-ecs" not in prod
        assert "elasticache" in prod

    def test_vpc_outputs_still_export_redis_sg(self):
        """VPC should still export redis_security_group_id (used by prod)."""
        outputs = read_tf(MODULES_DIR / "vpc" / "outputs.tf")
        assert "redis_security_group_id" in outputs
