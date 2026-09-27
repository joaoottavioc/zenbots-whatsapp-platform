"""PII masking helpers shared by logging, error handling and Sentry.

Dependency-free on purpose: imported by app/sentry_setup.py before the rest
of the app is loaded. `mask_phone` is re-exported from app.utils for the
existing call sites.
"""

import re
from typing import Any

FILTERED = "[Filtered]"

# Brazilian phone numbers as they appear in payloads and error text:
# 5511999990000, +55 (11) 99999-0000, 11 99999-0000, (11) 3333-4444 ...
_PHONE_RE = re.compile(
    r"(?<![\w])(?:\+?55[\s-]?)?\(?\d{2}\)?[\s-]?9?\d{4}[\s-]?\d{4}(?![\w])"
)
# CEP: 04674-225 / 04674225
_CEP_RE = re.compile(r"(?<![\w])\d{5}-?\d{3}(?![\w])")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Bearer tokens / long opaque secrets that end up in exception text.
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[\w\-.~+/=]+")
# Credentials written inline as key=value / "key": "value", as HTTP client
# and Graph API errors do (e.g. "...?access_token=EAAG...").
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b([\w-]*(?:token|secret|password|passwd|api[_-]?key|signature)[\w-]*)"
    # Value runs to whitespace/&/quote/;/} — commas included, since MP's
    # x-signature is "ts=...,v1=...".
    r"(\"?\s*[=:]\s*\"?)([^\s&\"';}]+)"
)

# Keys whose values are credentials: always dropped.
_SECRET_KEY_RE = re.compile(
    r"(?i)(token|secret|password|passwd|authorization|api[_-]?key|cookie|"
    r"signature|encryption[_-]?key|private[_-]?key|dsn|credential|session[_-]?key)"
)
# Keys whose values are customer message content: always dropped.
_CONTENT_KEYS = {
    "body",
    "text",
    "text_body",
    "message_text",
    "messages",
    "content",
    "transcript",
    "caption",
    "prompt",
    "user_query",
    "response_text",
    "response_to_user",
    "history",
}
# Keys whose values are delivery addresses: always dropped.
_ADDRESS_KEYS = {
    "cep",
    "address",
    "full_address",
    "delivery_address",
    "street",
    "logradouro",
    "bairro",
    "neighborhood",
    "complement",
    "complemento",
    "numero",
    "address_number",
    "house_number",
    "city",
    "localidade",
    "latitude",
    "longitude",
    "lat",
    "lng",
}
# Keys whose values are phone numbers: masked (keeps DDD + last 2 digits,
# which is enough to correlate without identifying the customer).
_PHONE_KEYS = {
    "phone",
    "phone_number",
    "contact_number",
    "contact_phone",
    "customer_phone",
    "from",
    "to",
    "wa_id",
    "display_phone_number",
}
# Keys whose values are customer names: dropped.
_NAME_KEYS = {"customer_name", "contact_name", "profile_name", "name_of_customer"}

_MAX_DEPTH = 12


def mask_phone(phone: str) -> str:
    """Mask a phone number, keeping first 4 and last 2 digits."""
    digits = re.sub(r"[^\d]", "", phone)
    if len(digits) <= 6:
        return "***"
    return digits[:4] + "***" + digits[-2:]


def scrub_text(value: str) -> str:
    """Mask phone numbers, CEPs, emails and bearer tokens inside free text."""
    if not value:
        return value
    value = _BEARER_RE.sub("Bearer " + FILTERED, value)
    value = _INLINE_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + FILTERED, value)
    value = _EMAIL_RE.sub("***@***", value)
    value = _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), value)
    value = _CEP_RE.sub("*****-***", value)
    return value


def _scrub_by_key(key: str, value: Any) -> tuple[bool, Any]:
    """Return (handled, new_value) for a dict entry based on its key."""
    k = key.lower()
    if _SECRET_KEY_RE.search(k):
        return True, FILTERED
    if k in _CONTENT_KEYS or k in _ADDRESS_KEYS or k in _NAME_KEYS:
        return True, FILTERED
    if k in _PHONE_KEYS and isinstance(value, (str, int)):
        return True, mask_phone(str(value))
    return False, value


def scrub_data(value: Any, _depth: int = 0) -> Any:
    """Recursively scrub a JSON-like structure (dicts, lists, strings)."""
    if _depth > _MAX_DEPTH:
        return FILTERED
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            handled, new = _scrub_by_key(str(k), v)
            out[k] = new if handled else scrub_data(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(scrub_data(v, _depth + 1) for v in value)
    if isinstance(value, str):
        return scrub_text(value)
    return value
