# app/sanitize.py
"""
Sanitize LLM-generated text before sending to WhatsApp users.

Strips URLs, emails, phone numbers, and dangerous Unicode characters
to prevent prompt-injection attacks from producing phishing content.
"""

import re

MAX_LENGTH = 1000

# Dangerous Unicode: RTL override, zero-width chars, bidi controls
_DANGEROUS_UNICODE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff\u00ad]"
)

# URLs (http, https, www)
_URL_PATTERN = re.compile(
    r"https?://[^\s]+|www\.[^\s]+",
    re.IGNORECASE,
)

# Email addresses
_EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

# Phone numbers: sequences of 8+ digits (possibly with separators)
# We use a pattern that matches digit sequences of 8+ digits, allowing
# optional separators like spaces, dashes, dots, or parentheses.
_PHONE_PATTERN = re.compile(
    r"(?<!\d)"                        # not preceded by a digit
    r"[\+]?"                          # optional leading +
    r"(?:\(?\d[\d\s\-\.\(\)]{6,}\d)" # 8+ digits with optional separators
    r"(?!\d)"                         # not followed by a digit
)


def sanitize_llm_output(text: str) -> str:
    """
    Sanitize LLM-generated text for safe WhatsApp delivery.

    - Strips dangerous Unicode (RTL override, zero-width chars)
    - Replaces URLs with [link removido]
    - Replaces email addresses with [email removido]
    - Replaces phone numbers (8+ digits) with [numero removido]
    - Collapses excess whitespace
    - Truncates to MAX_LENGTH chars
    """
    if not text:
        return ""

    # 1. Strip dangerous Unicode
    text = _DANGEROUS_UNICODE.sub("", text)

    # 2. Replace URLs
    text = _URL_PATTERN.sub("[link removido]", text)

    # 3. Replace emails
    text = _EMAIL_PATTERN.sub("[email removido]", text)

    # 4. Replace phone numbers (8+ digit sequences)
    text = _PHONE_PATTERN.sub("[numero removido]", text)

    # 5. Collapse excess whitespace (keep single newlines)
    text = re.sub(r"[^\S\n]+", " ", text)      # spaces/tabs → single space
    text = re.sub(r"\n{3,}", "\n\n", text)      # 3+ newlines → 2

    # 6. Truncate
    if len(text) > MAX_LENGTH:
        text = text[:MAX_LENGTH] + "..."

    return text.strip()
