#!/usr/bin/env bash
#
# Validates that all required SSM parameters exist and are non-empty.
# Run after migrate_secrets_to_ssm.sh to verify the migration.
#
# Usage:
#   bash scripts/validate_ssm_params.sh [environment]

set -euo pipefail

ENV="${1:-dev}"
PROJECT="zenbots"
REGION="us-east-1"

echo "=== Validating SSM Parameters for ${PROJECT}/${ENV} ==="
echo ""

EXPECTED_PARAMS=(
  "/${PROJECT}/${ENV}/database/url"
  "/${PROJECT}/${ENV}/auth/secret_key"
  "/${PROJECT}/${ENV}/auth/encryption_key"
  "/${PROJECT}/${ENV}/openai/api_key"
  "/${PROJECT}/${ENV}/whatsapp/token"
  "/${PROJECT}/${ENV}/whatsapp/verify_token"
  "/${PROJECT}/${ENV}/whatsapp/fb_app_id"
  "/${PROJECT}/${ENV}/whatsapp/fb_app_secret"
  "/${PROJECT}/${ENV}/mercadopago/client_id"
  "/${PROJECT}/${ENV}/mercadopago/client_secret"
  "/${PROJECT}/${ENV}/mercadopago/admin_token"
  "/${PROJECT}/${ENV}/google/maps_api_key"
  "/${PROJECT}/${ENV}/email/server"
  "/${PROJECT}/${ENV}/email/port"
  "/${PROJECT}/${ENV}/email/username"
  "/${PROJECT}/${ENV}/email/password"
  "/${PROJECT}/${ENV}/email/from"
  "/${PROJECT}/${ENV}/email/starttls"
  "/${PROJECT}/${ENV}/email/use_credentials"
)

PASS=0
FAIL=0

for param in "${EXPECTED_PARAMS[@]}"; do
  echo -n "  ${param} ... "

  VALUE=$(aws ssm get-parameter \
    --name "${param}" \
    --with-decryption \
    --region "${REGION}" \
    --query "Parameter.Value" \
    --output text 2>/dev/null || echo "")

  if [[ -z "${VALUE}" || "${VALUE}" == "CHANGE_ME" ]]; then
    echo "FAIL (empty or placeholder)"
    ((FAIL++))
  else
    # Show first 3 chars only for verification (don't leak secrets)
    PREVIEW="${VALUE:0:3}***"
    echo "OK (${PREVIEW})"
    ((PASS++))
  fi
done

echo ""
echo "=== Results ==="
echo "  Passed: ${PASS}/${#EXPECTED_PARAMS[@]}"
echo "  Failed: ${FAIL}/${#EXPECTED_PARAMS[@]}"

if [[ ${FAIL} -gt 0 ]]; then
  echo ""
  echo "WARNING: Some parameters are missing or still have placeholder values."
  echo "Run scripts/migrate_secrets_to_ssm.sh to populate them."
  exit 1
fi

echo ""
echo "All parameters validated successfully."
