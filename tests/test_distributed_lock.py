# tests/test_distributed_lock.py
"""
Unit tests for the distributed lock module.
"""

from unittest.mock import patch, MagicMock


class TestContactLock:
    """Tests for app.distributed_lock.contact_lock()."""

    def _make_mock_client(self):
        client = MagicMock()
        client.lock = MagicMock(return_value=MagicMock())
        return client

    def test_lock_uses_correct_key_format(self):
        """Lock key must be 'contact_lock:{contact_id}'."""
        mock_client = self._make_mock_client()

        with patch("app.distributed_lock._get_client", return_value=mock_client):
            from app.distributed_lock import contact_lock

            contact_lock(42)

        mock_client.lock.assert_called_once()
        call_kwargs = mock_client.lock.call_args
        assert call_kwargs.kwargs["name"] == "contact_lock:42"

    def test_lock_parameters_match_spec(self):
        """TTL=90s, blocking_timeout=95s, sleep=0.5s."""
        mock_client = self._make_mock_client()

        with patch("app.distributed_lock._get_client", return_value=mock_client):
            from app.distributed_lock import contact_lock

            contact_lock(1)

        kwargs = mock_client.lock.call_args.kwargs
        assert kwargs["timeout"] == 90
        assert kwargs["blocking_timeout"] == 95
        assert kwargs["sleep"] == 0.5

    def test_singleton_client_reused(self):
        """_get_client() returns the same instance on subsequent calls."""
        import app.distributed_lock as dl

        dl._client = None  # Reset singleton

        with patch("app.distributed_lock.redis") as mock_redis:
            mock_instance = MagicMock()
            mock_redis.from_url.return_value = mock_instance

            c1 = dl._get_client()
            c2 = dl._get_client()

        assert c1 is c2
        mock_redis.from_url.assert_called_once()

        # Cleanup
        dl._client = None

    def test_different_contacts_get_different_keys(self):
        """Each contact_id must produce a unique lock key."""
        mock_client = self._make_mock_client()

        with patch("app.distributed_lock._get_client", return_value=mock_client):
            from app.distributed_lock import contact_lock

            contact_lock(10)
            contact_lock(20)

        calls = mock_client.lock.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["name"] == "contact_lock:10"
        assert calls[1].kwargs["name"] == "contact_lock:20"
