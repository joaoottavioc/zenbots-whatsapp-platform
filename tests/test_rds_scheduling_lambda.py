"""Unit tests for the RDS stop/start scheduling Lambda function.

Tests the Lambda handler logic that EventBridge Scheduler invokes
to stop/start the dev RDS instance outside business hours (Phase 2).
"""

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

# The Lambda code is inline in Terraform, so we replicate it here as a module
# to test the exact same logic. Keep this in sync with
# infra/modules/rds-scheduling/main.tf

LAMBDA_CODE = """
import json
import logging
import boto3
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rds = boto3.client("rds")
INSTANCE_ID = os.environ["RDS_INSTANCE_ID"]


def get_instance_status():
    resp = rds.describe_db_instances(DBInstanceIdentifier=INSTANCE_ID)
    return resp["DBInstances"][0]["DBInstanceStatus"]


def handler(event, context):
    action = event.get("action")
    if action not in ("start", "stop"):
        raise ValueError(f"Invalid action: {action}. Must be \\'start\\' or \\'stop\\'.")

    status = get_instance_status()
    logger.info(f"RDS {INSTANCE_ID} current status: {status}, requested action: {action}")

    if action == "stop":
        if status == "stopped":
            logger.info("Instance already stopped, skipping.")
            return {"status": "already_stopped"}
        if status != "available":
            logger.warning(f"Cannot stop instance in state \\'{status}\\', skipping.")
            return {"status": "skipped", "reason": f"instance_state_{status}"}
        rds.stop_db_instance(DBInstanceIdentifier=INSTANCE_ID)
        logger.info("Stop command sent successfully.")
        return {"status": "stopping"}

    if action == "start":
        if status == "available":
            logger.info("Instance already available, skipping.")
            return {"status": "already_available"}
        if status != "stopped":
            logger.warning(f"Cannot start instance in state \\'{status}\\', skipping.")
            return {"status": "skipped", "reason": f"instance_state_{status}"}
        rds.start_db_instance(DBInstanceIdentifier=INSTANCE_ID)
        logger.info("Start command sent successfully.")
        return {"status": "starting"}
"""

INSTANCE_ID = "zenbots-dev"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("RDS_INSTANCE_ID", INSTANCE_ID)


@pytest.fixture()
def mock_rds():
    with patch("boto3.client") as mock_client:
        rds_mock = MagicMock()
        mock_client.return_value = rds_mock
        yield rds_mock


@pytest.fixture()
def lambda_module(mock_rds, tmp_path):
    """Import the Lambda code as a module, with boto3.client mocked."""
    code_path = tmp_path / "lambda_function.py"
    # Write clean code (unescape the quotes)
    clean_code = LAMBDA_CODE.replace("\\'", "'")
    code_path.write_text(clean_code)
    sys.path.insert(0, str(tmp_path))
    try:
        # Force fresh import each test
        if "lambda_function" in sys.modules:
            del sys.modules["lambda_function"]
        mod = importlib.import_module("lambda_function")
        # Point module's rds client to our mock
        mod.rds = mock_rds
        mod.INSTANCE_ID = INSTANCE_ID
        yield mod
    finally:
        sys.path.pop(0)
        if "lambda_function" in sys.modules:
            del sys.modules["lambda_function"]


# --- Stop action tests ---


class TestStopAction:
    def test_stop_when_available(self, lambda_module, mock_rds):
        """Lambda should call stop_db_instance when RDS is available."""
        mock_rds.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceStatus": "available"}]
        }

        result = lambda_module.handler({"action": "stop"}, None)

        assert result == {"status": "stopping"}
        mock_rds.stop_db_instance.assert_called_once_with(
            DBInstanceIdentifier=INSTANCE_ID
        )

    def test_stop_when_already_stopped(self, lambda_module, mock_rds):
        """Lambda should skip if RDS is already stopped."""
        mock_rds.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceStatus": "stopped"}]
        }

        result = lambda_module.handler({"action": "stop"}, None)

        assert result == {"status": "already_stopped"}
        mock_rds.stop_db_instance.assert_not_called()

    def test_stop_when_starting(self, lambda_module, mock_rds):
        """Lambda should skip stop if RDS is in a transitional state."""
        mock_rds.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceStatus": "starting"}]
        }

        result = lambda_module.handler({"action": "stop"}, None)

        assert result == {"status": "skipped", "reason": "instance_state_starting"}
        mock_rds.stop_db_instance.assert_not_called()

    def test_stop_when_stopping(self, lambda_module, mock_rds):
        """Lambda should skip stop if RDS is already stopping."""
        mock_rds.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceStatus": "stopping"}]
        }

        result = lambda_module.handler({"action": "stop"}, None)

        assert result == {"status": "skipped", "reason": "instance_state_stopping"}
        mock_rds.stop_db_instance.assert_not_called()


# --- Start action tests ---


class TestStartAction:
    def test_start_when_stopped(self, lambda_module, mock_rds):
        """Lambda should call start_db_instance when RDS is stopped."""
        mock_rds.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceStatus": "stopped"}]
        }

        result = lambda_module.handler({"action": "start"}, None)

        assert result == {"status": "starting"}
        mock_rds.start_db_instance.assert_called_once_with(
            DBInstanceIdentifier=INSTANCE_ID
        )

    def test_start_when_already_available(self, lambda_module, mock_rds):
        """Lambda should skip if RDS is already available."""
        mock_rds.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceStatus": "available"}]
        }

        result = lambda_module.handler({"action": "start"}, None)

        assert result == {"status": "already_available"}
        mock_rds.start_db_instance.assert_not_called()

    def test_start_when_stopping(self, lambda_module, mock_rds):
        """Lambda should skip start if RDS is in a transitional state."""
        mock_rds.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceStatus": "stopping"}]
        }

        result = lambda_module.handler({"action": "start"}, None)

        assert result == {"status": "skipped", "reason": "instance_state_stopping"}
        mock_rds.start_db_instance.assert_not_called()

    def test_start_when_modifying(self, lambda_module, mock_rds):
        """Lambda should skip start if RDS is being modified."""
        mock_rds.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceStatus": "modifying"}]
        }

        result = lambda_module.handler({"action": "start"}, None)

        assert result == {"status": "skipped", "reason": "instance_state_modifying"}
        mock_rds.start_db_instance.assert_not_called()


# --- Validation tests ---


class TestValidation:
    def test_invalid_action_raises(self, lambda_module, mock_rds):
        """Lambda should raise ValueError for unknown actions."""
        with pytest.raises(ValueError, match="Invalid action"):
            lambda_module.handler({"action": "restart"}, None)

    def test_missing_action_raises(self, lambda_module, mock_rds):
        """Lambda should raise ValueError when action is missing."""
        with pytest.raises(ValueError, match="Invalid action"):
            lambda_module.handler({}, None)

    def test_none_action_raises(self, lambda_module, mock_rds):
        """Lambda should raise ValueError when action is None."""
        with pytest.raises(ValueError, match="Invalid action"):
            lambda_module.handler({"action": None}, None)
