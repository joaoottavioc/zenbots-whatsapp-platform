#!/usr/bin/env python3
"""
Migrates secret values from AWS Secrets Manager to SSM Parameter Store.
Run AFTER terraform apply has created the SSM parameters with placeholders.

Usage:
    python scripts/migrate_secrets_to_ssm.py [environment]

Default environment: dev
"""

import json
import subprocess
import sys

ENV = sys.argv[1] if len(sys.argv) > 1 else "dev"
PROJECT = "zenbots"
REGION = "us-east-1"

# Mapping: (sm_secret_name, json_key) -> ssm_parameter_path
MAPPINGS = {
    ("database", "url"): "database/url",
    ("auth", "secret_key"): "auth/secret_key",
    ("auth", "encryption_key"): "auth/encryption_key",
    ("openai", "api_key"): "openai/api_key",
    ("whatsapp", "token"): "whatsapp/token",
    ("whatsapp", "verify_token"): "whatsapp/verify_token",
    ("whatsapp", "fb_app_id"): "whatsapp/fb_app_id",
    ("whatsapp", "fb_app_secret"): "whatsapp/fb_app_secret",
    ("mercadopago", "client_id"): "mercadopago/client_id",
    ("mercadopago", "client_secret"): "mercadopago/client_secret",
    ("mercadopago", "admin_token"): "mercadopago/admin_token",
    ("google", "maps_api_key"): "google/maps_api_key",
    ("email", "server"): "email/server",
    ("email", "port"): "email/port",
    ("email", "username"): "email/username",
    ("email", "password"): "email/password",
    ("email", "from"): "email/from",
    ("email", "starttls"): "email/starttls",
    ("email", "use_credentials"): "email/use_credentials",
}


def get_secret(secret_id: str) -> dict:
    """Fetch a secret from Secrets Manager and return parsed JSON."""
    result = subprocess.run(
        [
            "aws", "secretsmanager", "get-secret-value",
            "--secret-id", secret_id,
            "--region", REGION,
            "--query", "SecretString",
            "--output", "text",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    return json.loads(result.stdout.strip())


def put_ssm_parameter(name: str, value: str) -> bool:
    """Write a value to SSM Parameter Store."""
    result = subprocess.run(
        [
            "aws", "ssm", "put-parameter",
            "--name", name,
            "--value", value,
            "--type", "SecureString",
            "--overwrite",
            "--region", REGION,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def main():
    print("=== Migrating secrets from Secrets Manager to SSM Parameter Store ===")
    print(f"Environment: {ENV}")
    print(f"Region:      {REGION}")
    print()

    # Cache SM secrets to avoid re-fetching the same secret multiple times
    sm_cache: dict[str, dict] = {}
    success = 0
    failed = 0

    for (sm_name, json_key), ssm_path in sorted(MAPPINGS.items()):
        sm_secret_id = f"{PROJECT}/{ENV}/{sm_name}"
        ssm_param_name = f"/{PROJECT}/{ENV}/{ssm_path}"

        print(f"  {sm_secret_id} [{json_key}] -> {ssm_param_name} ... ", end="", flush=True)

        # Get the SM secret (cached)
        if sm_name not in sm_cache:
            sm_cache[sm_name] = get_secret(sm_secret_id)

        secret_data = sm_cache[sm_name]
        value = secret_data.get(json_key, "")

        if not value:
            print("SKIP (empty or missing key)")
            failed += 1
            continue

        if put_ssm_parameter(ssm_param_name, str(value)):
            print("OK")
            success += 1
        else:
            print("FAIL (SSM write error)")
            failed += 1

    print()
    print("=== Migration complete ===")
    print(f"  Success: {success}")
    print(f"  Skipped: {failed}")
    print()
    print("Next steps:")
    print("  1. Run: python scripts/validate_ssm_params.py dev")
    print("  2. Force ECS redeploy or push to develop")
    print("  3. Verify /health/ready returns 200")
    print("  4. After validation, remove Secrets Manager module from dev (separate PR)")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
