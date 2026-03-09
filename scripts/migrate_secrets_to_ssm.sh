#!/usr/bin/env bash
#
# Migrates secret values from AWS Secrets Manager to SSM Parameter Store.
# Run AFTER terraform apply has created the SSM parameters with placeholders.
#
# Usage:
#   export AWS_PROFILE=your-profile   # or use OIDC/default credentials
#   bash scripts/migrate_secrets_to_ssm.sh [environment]
#
# Default environment: dev

set -euo pipefail

ENV="${1:-dev}"
PROJECT="zenbots"
REGION="us-east-1"

echo "=== Migrating secrets from Secrets Manager to SSM Parameter Store ==="
echo "Environment: ${ENV}"
echo "Region:      ${REGION}"
echo ""

# Mapping: "sm_secret_name:json_key -> ssm_parameter_path"
declare -A MAPPINGS=(
  ["database:url"]="database/url"
  ["auth:secret_key"]="auth/secret_key"
  ["auth:encryption_key"]="auth/encryption_key"
  ["openai:api_key"]="openai/api_key"
  ["whatsapp:token"]="whatsapp/token"
  ["whatsapp:verify_token"]="whatsapp/verify_token"
  ["whatsapp:fb_app_id"]="whatsapp/fb_app_id"
  ["whatsapp:fb_app_secret"]="whatsapp/fb_app_secret"
  ["mercadopago:client_id"]="mercadopago/client_id"
  ["mercadopago:client_secret"]="mercadopago/client_secret"
  ["mercadopago:admin_token"]="mercadopago/admin_token"
  ["google:maps_api_key"]="google/maps_api_key"
  ["email:server"]="email/server"
  ["email:port"]="email/port"
  ["email:username"]="email/username"
  ["email:password"]="email/password"
  ["email:from"]="email/from"
  ["email:starttls"]="email/starttls"
  ["email:use_credentials"]="email/use_credentials"
)

SUCCESS=0
FAILED=0

for source in "${!MAPPINGS[@]}"; do
  ssm_path="${MAPPINGS[$source]}"
  sm_name="${source%%:*}"
  json_key="${source##*:}"

  sm_secret_id="${PROJECT}/${ENV}/${sm_name}"
  ssm_param_name="/${PROJECT}/${ENV}/${ssm_path}"

  echo -n "  ${sm_secret_id} [${json_key}] -> ${ssm_param_name} ... "

  # Extract the value from Secrets Manager JSON (uses python instead of jq for portability)
  SECRET_JSON=$(aws secretsmanager get-secret-value \
    --secret-id "${sm_secret_id}" \
    --region "${REGION}" \
    --query SecretString \
    --output text 2>/dev/null || echo "")

  VALUE=$(python -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    val = data.get(sys.argv[2], '')
    print(val if val else '', end='')
except Exception:
    print('', end='')
" "${SECRET_JSON}" "${json_key}")

  if [[ -z "${VALUE}" ]]; then
    echo "SKIP (empty or missing key)"
    ((FAILED++))
    continue
  fi

  # Write to SSM Parameter Store
  aws ssm put-parameter \
    --name "${ssm_param_name}" \
    --value "${VALUE}" \
    --type SecureString \
    --overwrite \
    --region "${REGION}" \
    > /dev/null 2>&1

  echo "OK"
  ((SUCCESS++))
done

echo ""
echo "=== Migration complete ==="
echo "  Success: ${SUCCESS}"
echo "  Skipped: ${FAILED}"
echo ""
echo "Next steps:"
echo "  1. Deploy new ECS task definitions (push to develop or manual ECS deploy)"
echo "  2. Verify /health/ready returns 200"
echo "  3. After validation, remove Secrets Manager module from dev (separate PR)"
