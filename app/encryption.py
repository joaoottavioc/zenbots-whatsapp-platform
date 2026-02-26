# app/encryption.py
"""
Fernet symmetric encryption for sensitive values stored at rest
(e.g. Mercado Pago access tokens).

Requires the ENCRYPTION_KEY environment variable to be a valid Fernet key.
Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import os

from cryptography.fernet import Fernet, InvalidToken

_KEY = os.getenv("ENCRYPTION_KEY")
_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.getenv("ENCRYPTION_KEY") or _KEY
        if not key:
            raise RuntimeError(
                "ENCRYPTION_KEY environment variable is not set. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns empty string for empty input."""
    if not plaintext:
        return ""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """
    Decrypt a ciphertext string. Returns empty string for empty input.

    Handles legacy plaintext tokens gracefully: if decryption fails
    (because the value was stored before encryption was enabled),
    the original value is returned as-is so existing tokens keep working.
    """
    if not ciphertext:
        return ""
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except (InvalidToken, Exception):
        # Legacy plaintext token — return as-is.
        # This path will disappear once all tokens are re-encrypted
        # (e.g. after the merchant re-authenticates with Mercado Pago).
        return ciphertext
