# tests/test_auth.py
"""
Unit tests for pure functions and Pydantic validators in app/auth.py.

Covered:
- RegisterRequest.password_complexity  (all 4 validation rules + valid password)
- create_access_token / jwt.decode      (token round-trip with known secret)
- verify_password / get_password_hash   (bcrypt hash + verify)

NOT covered here: HTTP endpoints (register, login, etc.)
"""
import pytest
from datetime import timedelta
from unittest.mock import patch

from jose import jwt
from pydantic import ValidationError

from app.auth import (
    RegisterRequest,
    ResetPasswordRequest,
    ChangePasswordRequest,
    validate_password_strength,
    create_access_token,
    get_password_hash,
    verify_password,
    ALGORITHM,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_SECRET = "test-secret-key-for-testing"
VALID_EMAIL = "test@example.com"
VALID_PASSWORD = "StrongPass1!"


# ===========================================================================
# RegisterRequest — password_complexity validator
# ===========================================================================

class TestPasswordComplexityValidator:
    """Each test exercises exactly one failing rule so failures are unambiguous."""

    def _make_request(self, password: str) -> RegisterRequest:
        return RegisterRequest(email=VALID_EMAIL, password=password)

    # --- Rule 1: minimum length ---

    def test_password_too_short_raises(self):
        """Passwords with fewer than 8 characters must be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            self._make_request("Ab1!")
        errors = exc_info.value.errors()
        assert any("8 caracteres" in e["msg"] for e in errors)

    def test_password_exactly_8_chars_but_valid_passes(self):
        """A password of exactly 8 characters that meets all rules is accepted."""
        req = self._make_request("Abcde1g!")
        assert req.password == "Abcde1g!"

    # --- Rule 2: at least one lowercase letter ---

    def test_password_no_lowercase_raises(self):
        """Passwords without a lowercase letter must be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            self._make_request("ALLCAPS1!")
        errors = exc_info.value.errors()
        assert any("minúscula" in e["msg"] for e in errors)

    # --- Rule 3: at least one uppercase letter ---

    def test_password_no_uppercase_raises(self):
        """Passwords without an uppercase letter must be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            self._make_request("alllower1!")
        errors = exc_info.value.errors()
        assert any("maiúscula" in e["msg"] for e in errors)

    # --- Rule 4: at least one digit ---

    def test_password_no_digit_raises(self):
        """Passwords without a digit must be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            self._make_request("NoDigitsHere!")
        errors = exc_info.value.errors()
        assert any("número" in e["msg"] for e in errors)

    # --- Rule 5: at least one special character ---

    def test_password_no_special_char_raises(self):
        """Passwords without a special character must be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            self._make_request("StrongPass1")
        errors = exc_info.value.errors()
        assert any("especial" in e["msg"] for e in errors)

    # --- Rule 6: common password blocklist ---

    def test_common_password_rejected(self):
        """A password from the common blocklist must be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            self._make_request("Trustno1!")
        errors = exc_info.value.errors()
        assert any("comum" in e["msg"] for e in errors)

    # --- Valid password ---

    def test_valid_password_is_accepted(self):
        """A password satisfying all rules must be stored unchanged."""
        req = self._make_request(VALID_PASSWORD)
        assert req.password == VALID_PASSWORD
        assert req.email == VALID_EMAIL

    # --- Edge cases ---

    def test_password_with_special_characters_passes(self):
        """Special characters should not interfere with validation."""
        req = self._make_request("Secure@Pass9!")
        assert req.password == "Secure@Pass9!"

    def test_empty_password_raises(self):
        """An empty string must be rejected (fails the length rule first)."""
        with pytest.raises(ValidationError) as exc_info:
            self._make_request("")
        errors = exc_info.value.errors()
        assert any("8 caracteres" in e["msg"] for e in errors)

    # --- Applied to all password schemas ---

    def test_reset_password_validates_strength(self):
        """ResetPasswordRequest also validates new_password."""
        with pytest.raises(ValidationError):
            ResetPasswordRequest(token="t", new_password="weak")

    def test_change_password_validates_strength(self):
        """ChangePasswordRequest also validates new_password."""
        with pytest.raises(ValidationError):
            ChangePasswordRequest(current_password="old", new_password="weak")


# ===========================================================================
# create_access_token and jwt.decode round-trip
# ===========================================================================

class TestCreateAccessToken:
    """Tests the JWT creation function with a deterministic secret."""

    @patch("app.auth.SECRET_KEY", TEST_SECRET)
    def test_token_contains_correct_sub_claim(self):
        """The decoded token must carry exactly the 'sub' value that was encoded."""
        token = create_access_token(data={"sub": "user@example.com"})
        payload = jwt.decode(token, TEST_SECRET, algorithms=[ALGORITHM])
        assert payload["sub"] == "user@example.com"

    @patch("app.auth.SECRET_KEY", TEST_SECRET)
    def test_token_contains_exp_claim(self):
        """Every generated token must include an expiration timestamp."""
        token = create_access_token(data={"sub": "user@example.com"})
        payload = jwt.decode(token, TEST_SECRET, algorithms=[ALGORITHM])
        assert "exp" in payload

    @patch("app.auth.SECRET_KEY", TEST_SECRET)
    def test_custom_expires_delta_is_respected(self):
        """When a custom timedelta is supplied, the exp claim must reflect it."""
        from datetime import datetime, timezone

        delta = timedelta(minutes=15)
        before = datetime.utcnow()
        token = create_access_token(data={"sub": "user@example.com"}, expires_delta=delta)
        after = datetime.utcnow()

        payload = jwt.decode(token, TEST_SECRET, algorithms=[ALGORITHM])
        exp = datetime.utcfromtimestamp(payload["exp"])

        # exp must be between (before + delta) and (after + delta) with 5 s margin
        assert exp >= before + delta - timedelta(seconds=5)
        assert exp <= after + delta + timedelta(seconds=5)

    @patch("app.auth.SECRET_KEY", TEST_SECRET)
    def test_extra_claims_are_preserved(self):
        """Additional claims embedded in the data dict must survive the round-trip."""
        token = create_access_token(data={"sub": "u@x.com", "type": "reset"})
        payload = jwt.decode(token, TEST_SECRET, algorithms=[ALGORITHM])
        assert payload["type"] == "reset"
        assert payload["sub"] == "u@x.com"

    @patch("app.auth.SECRET_KEY", TEST_SECRET)
    def test_token_signed_with_wrong_key_raises(self):
        """A token decoded with the wrong secret must raise a JWTError."""
        from jose import JWTError

        token = create_access_token(data={"sub": "u@x.com"})
        with pytest.raises(JWTError):
            jwt.decode(token, "wrong-secret", algorithms=[ALGORITHM])

    @patch("app.auth.SECRET_KEY", TEST_SECRET)
    def test_default_expiry_is_24_hours(self):
        """Without a custom delta the token must expire approximately 24 hours from now."""
        from datetime import datetime

        token = create_access_token(data={"sub": "u@x.com"})
        payload = jwt.decode(token, TEST_SECRET, algorithms=[ALGORITHM])
        exp = datetime.utcfromtimestamp(payload["exp"])
        now = datetime.utcnow()

        # Allow ±60 s tolerance for test execution time
        expected_seconds = 60 * 60 * 24
        delta_seconds = (exp - now).total_seconds()
        assert abs(delta_seconds - expected_seconds) < 60


# ===========================================================================
# verify_password / get_password_hash
# ===========================================================================

class TestPasswordHashing:
    """Tests the bcrypt hashing and verification utilities."""

    def test_hash_differs_from_plaintext(self):
        """The hashed value must never equal the original plain text."""
        hashed = get_password_hash("MySecret1")
        assert hashed != "MySecret1"

    def test_verify_correct_password_returns_true(self):
        """verify_password must return True when the plain password matches the hash."""
        plain = "MySecret1"
        hashed = get_password_hash(plain)
        assert verify_password(plain, hashed) is True

    def test_verify_wrong_password_returns_false(self):
        """verify_password must return False when the plain password is incorrect."""
        hashed = get_password_hash("CorrectHorse1")
        assert verify_password("WrongHorse1", hashed) is False

    def test_same_password_produces_different_hashes(self):
        """bcrypt salts ensure two hashes of the same password differ."""
        hashed_a = get_password_hash("SamePass1")
        hashed_b = get_password_hash("SamePass1")
        assert hashed_a != hashed_b

    def test_both_hashes_verify_correctly(self):
        """Despite different salts, both hashes of the same password must verify."""
        plain = "SamePass1"
        hashed_a = get_password_hash(plain)
        hashed_b = get_password_hash(plain)
        assert verify_password(plain, hashed_a) is True
        assert verify_password(plain, hashed_b) is True

    def test_hash_is_a_non_empty_string(self):
        """get_password_hash must always return a non-empty string."""
        hashed = get_password_hash("AnyPass1")
        assert isinstance(hashed, str)
        assert len(hashed) > 0

    def test_verify_empty_string_against_hash_returns_false(self):
        """An empty string must not match a hash of a real password."""
        hashed = get_password_hash("RealPass1")
        assert verify_password("", hashed) is False
