# tests/test_worker.py
"""
Unit tests for ARQ WorkerSettings in app/worker.py.

Verifies that retry, timeout, and error-handling settings are configured.
"""

from unittest.mock import patch

import pytest

from app.embedding_service import EmbeddingsUnavailable
from app.worker import WorkerSettings


class TestWorkerSettings:
    def test_max_tries_is_at_least_two(self):
        assert WorkerSettings.max_tries >= 2

    def test_job_timeout_is_at_least_60_seconds(self):
        assert WorkerSettings.job_timeout >= 60

    def test_keep_result_is_positive(self):
        assert WorkerSettings.keep_result > 0

    def test_on_job_error_exists_and_is_callable(self):
        assert hasattr(WorkerSettings, "on_job_error")
        assert callable(WorkerSettings.on_job_error)


class TestWorkerOnStartup:
    @pytest.mark.asyncio
    async def test_pre_warms_embedding_model(self):
        with (
            patch("app.worker.setup_logging"),
            patch("app.worker.start_flush_task"),
            patch("app.embedding_service._get_model") as mock_get_model,
        ):
            await WorkerSettings.on_startup(WorkerSettings())
            mock_get_model.assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_survives_embedding_load_failure(self):
        """A pre-warm failure must not crash worker startup — the rest of
        the message pipeline degrades gracefully without embeddings."""
        with (
            patch("app.worker.setup_logging"),
            patch("app.worker.start_flush_task"),
            patch(
                "app.embedding_service._get_model",
                side_effect=EmbeddingsUnavailable("boom"),
            ),
        ):
            await WorkerSettings.on_startup(WorkerSettings())
