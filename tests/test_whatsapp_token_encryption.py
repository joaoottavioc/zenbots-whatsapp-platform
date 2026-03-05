# tests/test_whatsapp_token_encryption.py
"""
Source-level verification that all bot.whatsapp_token read sites in whatsapp.py
are wrapped with decrypt_value().
"""

import ast
import inspect

import app.whatsapp as whatsapp_module


def test_all_whatsapp_token_reads_use_decrypt_value():
    """Every reference to bot.whatsapp_token in whatsapp.py must be wrapped
    in decrypt_value() — raw attribute access is a security gap."""

    source = inspect.getsource(whatsapp_module)
    tree = ast.parse(source)

    raw_accesses = []

    for node in ast.walk(tree):
        # Look for attribute access: *.whatsapp_token
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr != "whatsapp_token":
            continue

        # We only care about read access (Load context), not assignments (Store)
        if not isinstance(node.ctx, ast.Load):
            continue

        # Check if the parent is a Call to decrypt_value
        # We walk again to find Call nodes whose args include this node
        # Simpler approach: check if the attribute's id chain matches common patterns
        # like bot.whatsapp_token or order.bot.whatsapp_token
        value = node.value
        is_bot_attr = False

        if isinstance(value, ast.Attribute) and value.attr == "bot":
            is_bot_attr = True  # e.g. order.bot.whatsapp_token
        elif isinstance(value, ast.Name) and value.id == "bot":
            is_bot_attr = True  # e.g. bot.whatsapp_token

        if not is_bot_attr:
            continue

        raw_accesses.append(node.lineno)

    # Now verify each raw access is inside a decrypt_value() call
    # Re-parse looking for decrypt_value(*.whatsapp_token) patterns
    decrypted_lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Check if it's a call to decrypt_value
        func = node.func
        if isinstance(func, ast.Name) and func.id == "decrypt_value":
            for arg in node.args:
                if isinstance(arg, ast.Attribute) and arg.attr == "whatsapp_token":
                    decrypted_lines.add(arg.lineno)

    unprotected = [line for line in raw_accesses if line not in decrypted_lines]
    assert not unprotected, (
        f"Found raw bot.whatsapp_token reads without decrypt_value() "
        f"at source lines: {unprotected}"
    )
