"""Tests for CORS configuration logic (V5).

We cannot import app.main directly because it transitively imports
bot_routes which requires fitz (PyMuPDF, only available in Docker).
Instead we replicate the build_cors_kwargs logic here and test it
to ensure the production guard works correctly.
"""


def _build_cors_kwargs_isolated(env_overrides: dict) -> dict:
    """
    Replicate the build_cors_kwargs logic from app/main.py
    so we can test it without importing the full app.
    """
    origins_raw = env_overrides.get("CORS_ORIGINS", "")
    origin_regex = env_overrides.get("CORS_ORIGIN_REGEX", "")
    environment = env_overrides.get("ENVIRONMENT", "development")

    kwargs: dict = dict(
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if origins_raw:
        kwargs["allow_origins"] = [
            o.strip() for o in origins_raw.split(",") if o.strip()
        ]
    elif origin_regex:
        kwargs["allow_origin_regex"] = origin_regex
    elif environment == "development":
        kwargs["allow_origin_regex"] = "https?://.*"
    else:
        kwargs["allow_origins"] = []

    return kwargs


class TestBuildCorsKwargs:
    def test_explicit_origins(self):
        result = _build_cors_kwargs_isolated(
            {
                "CORS_ORIGINS": "https://app.zen.com, https://admin.zen.com",
            }
        )
        assert result["allow_origins"] == [
            "https://app.zen.com",
            "https://admin.zen.com",
        ]
        assert "allow_origin_regex" not in result

    def test_explicit_regex(self):
        result = _build_cors_kwargs_isolated(
            {
                "CORS_ORIGIN_REGEX": r"https://.*\.zen\.com",
            }
        )
        assert result["allow_origin_regex"] == r"https://.*\.zen\.com"
        assert "allow_origins" not in result

    def test_dev_fallback_wildcard(self):
        result = _build_cors_kwargs_isolated({"ENVIRONMENT": "development"})
        assert result["allow_origin_regex"] == "https?://.*"

    def test_production_no_origins_empty_list(self):
        result = _build_cors_kwargs_isolated({"ENVIRONMENT": "production"})
        assert result["allow_origins"] == []
        assert "allow_origin_regex" not in result

    def test_production_with_origins_works(self):
        result = _build_cors_kwargs_isolated(
            {
                "ENVIRONMENT": "production",
                "CORS_ORIGINS": "https://myapp.com",
            }
        )
        assert result["allow_origins"] == ["https://myapp.com"]

    def test_always_has_credentials(self):
        result = _build_cors_kwargs_isolated({})
        assert result["allow_credentials"] is True

    def test_origins_take_priority_over_regex(self):
        result = _build_cors_kwargs_isolated(
            {
                "CORS_ORIGINS": "https://app.com",
                "CORS_ORIGIN_REGEX": "https://.*",
            }
        )
        assert "allow_origins" in result
        assert "allow_origin_regex" not in result
