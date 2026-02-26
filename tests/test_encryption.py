# tests/test_encryption.py
"""
Tests for app/encryption.py — Fernet-based encryption for sensitive values.
"""
import pytest
from unittest.mock import patch
from cryptography.fernet import Fernet, InvalidToken


# Generate a real test key for use in tests
TEST_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def reset_fernet():
    """Reset the module-level _fernet singleton between tests."""
    import app.encryption
    app.encryption._fernet = None
    yield
    app.encryption._fernet = None


class TestEncryptDecryptRoundTrip:
    def test_round_trip(self):
        """Encrypting then decrypting returns the original plaintext."""
        with patch.dict("os.environ", {"ENCRYPTION_KEY": TEST_KEY}):
            from app.encryption import encrypt_value, decrypt_value
            plaintext = "APP_USR-abc123-test-token"
            ciphertext = encrypt_value(plaintext)
            assert ciphertext != plaintext
            assert decrypt_value(ciphertext) == plaintext

    def test_different_ciphertexts_for_same_plaintext(self):
        """Fernet uses random IV, so encrypting the same value twice gives different results."""
        with patch.dict("os.environ", {"ENCRYPTION_KEY": TEST_KEY}):
            from app.encryption import encrypt_value
            plaintext = "test-token-123"
            c1 = encrypt_value(plaintext)
            c2 = encrypt_value(plaintext)
            assert c1 != c2  # Different IVs

    def test_empty_string_returns_empty(self):
        """Empty input returns empty output without calling Fernet."""
        with patch.dict("os.environ", {"ENCRYPTION_KEY": TEST_KEY}):
            from app.encryption import encrypt_value, decrypt_value
            assert encrypt_value("") == ""
            assert decrypt_value("") == ""


class TestWrongKeyFallback:
    def test_wrong_key_returns_ciphertext_as_fallback(self):
        """Decrypting with a different key falls back to returning the ciphertext (legacy support)."""
        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()

        with patch.dict("os.environ", {"ENCRYPTION_KEY": key1}):
            from app.encryption import encrypt_value
            ciphertext = encrypt_value("secret")

        # Reset singleton to use a different key
        import app.encryption
        app.encryption._fernet = None

        with patch.dict("os.environ", {"ENCRYPTION_KEY": key2}):
            from app.encryption import decrypt_value
            # Falls back to returning ciphertext as-is (legacy plaintext path)
            result = decrypt_value(ciphertext)
            assert result == ciphertext


class TestLegacyPlaintextFallback:
    def test_plaintext_token_returned_as_is(self):
        """A plaintext value (not encrypted) is returned unchanged for legacy compatibility."""
        with patch.dict("os.environ", {"ENCRYPTION_KEY": TEST_KEY}):
            from app.encryption import decrypt_value
            plaintext = "APP_USR-1234567890-abcdef"
            result = decrypt_value(plaintext)
            assert result == plaintext


class TestMissingKey:
    def test_missing_key_raises_runtime_error(self):
        """If ENCRYPTION_KEY is not set, RuntimeError must be raised."""
        with patch.dict("os.environ", {}, clear=True):
            # Ensure ENCRYPTION_KEY is not in env
            import os
            os.environ.pop("ENCRYPTION_KEY", None)

            import app.encryption
            app.encryption._fernet = None
            app.encryption._KEY = None

            with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
                app.encryption.encrypt_value("test")
