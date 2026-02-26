# tests/test_timeouts.py
"""
Verify that external API clients are created with explicit timeouts,
including Redis connections.
"""
import re
import inspect
import pytest
from unittest.mock import patch, MagicMock


class TestOpenAIClientTimeout:
    """Verify the OpenAI client is initialized with a timeout."""

    def test_openai_client_has_timeout(self):
        with patch("openai.OpenAI") as MockOpenAI:
            # Force re-import to trigger constructor
            import importlib
            import app.openai_client
            importlib.reload(app.openai_client)

            MockOpenAI.assert_called_once()
            call_kwargs = MockOpenAI.call_args
            assert call_kwargs.kwargs.get("timeout") == 30.0 or \
                   (len(call_kwargs.args) > 0 and call_kwargs.kwargs.get("timeout") == 30.0), \
                   "OpenAI client must be created with timeout=30.0"


class TestHttpxClientTimeouts:
    """Verify httpx.AsyncClient is always created with a timeout."""

    def test_bot_routes_uses_timeout(self):
        """All httpx.AsyncClient() in bot_routes.py must have timeout."""
        import inspect
        import app.bot_routes as mod
        source = inspect.getsource(mod)
        # Should NOT have bare AsyncClient() without timeout
        import re
        bare_calls = re.findall(r'httpx\.AsyncClient\(\)', source)
        assert len(bare_calls) == 0, (
            f"Found {len(bare_calls)} httpx.AsyncClient() without timeout in bot_routes.py"
        )

    def test_whatsapp_uses_timeout(self):
        """All httpx.AsyncClient() in whatsapp.py must have timeout."""
        import inspect
        import app.whatsapp as mod
        source = inspect.getsource(mod)
        import re
        bare_calls = re.findall(r'httpx\.AsyncClient\(\)', source)
        assert len(bare_calls) == 0, (
            f"Found {len(bare_calls)} httpx.AsyncClient() without timeout in whatsapp.py"
        )

    def test_payment_routes_uses_timeout(self):
        """All httpx.AsyncClient() in payment_routes.py must have timeout."""
        import inspect
        import app.payment_routes as mod
        source = inspect.getsource(mod)
        import re
        bare_calls = re.findall(r'httpx\.AsyncClient\(\)', source)
        assert len(bare_calls) == 0, (
            f"Found {len(bare_calls)} httpx.AsyncClient() without timeout in payment_routes.py"
        )

    def test_address_service_uses_timeout(self):
        """All httpx.AsyncClient() in address_service.py must have timeout."""
        import app.address_service as mod
        source = inspect.getsource(mod)
        bare_calls = re.findall(r'httpx\.AsyncClient\(\)', source)
        assert len(bare_calls) == 0, (
            f"Found {len(bare_calls)} httpx.AsyncClient() without timeout in address_service.py"
        )


class TestRedisConnectionTimeouts:
    """Verify all redis.from_url() calls include socket timeout params."""

    def _check_redis_timeouts_in_source(self, module_name: str):
        """Source-inspect a module and verify redis.from_url() has timeout params."""
        import pathlib
        # Use file-based source reading to avoid import side effects
        parts = module_name.split(".")
        module_path = pathlib.Path(__file__).resolve().parent.parent / "/".join(parts[:-1]) / f"{parts[-1]}.py"
        source = module_path.read_text(encoding="utf-8")
        # Find all redis.from_url() call blocks
        calls = list(re.finditer(r'redis\.from_url\(', source))
        assert len(calls) > 0, f"No redis.from_url() calls found in {module_name}"
        for match in calls:
            # Grab from the call start to the next unmatched closing paren
            start = match.start()
            depth = 0
            end = start
            for i in range(start, len(source)):
                if source[i] == '(':
                    depth += 1
                elif source[i] == ')':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            call_text = source[start:end]
            assert "socket_timeout" in call_text, (
                f"redis.from_url() in {module_name} missing socket_timeout:\n{call_text}"
            )
            assert "socket_connect_timeout" in call_text, (
                f"redis.from_url() in {module_name} missing socket_connect_timeout:\n{call_text}"
            )

    def test_rate_limiter_redis_has_timeouts(self):
        self._check_redis_timeouts_in_source("app.rate_limiter")

    def test_broadcast_redis_has_timeouts(self):
        self._check_redis_timeouts_in_source("app.broadcast")

    def test_main_redis_has_timeouts(self):
        self._check_redis_timeouts_in_source("app.main")
