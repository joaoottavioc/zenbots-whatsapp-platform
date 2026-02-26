# tests/test_worker.py
"""
Unit tests for ARQ WorkerSettings in app/worker.py.

Verifies that retry, timeout, and error-handling settings are configured.
"""
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
