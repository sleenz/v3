#!/usr/bin/env bash
# Build and deploy the service in one command.
#   deploy/deploy.sh                  run the tests, build from source, deploy
#   deploy/deploy.sh --skip-tests     the same without the local test run
#   deploy/deploy.sh --image IMAGE    deploy an image already built (cloudbuild.yaml uses this)
# Settings come from the secret grc-env-prod (deploy/env.prod.yaml) and the secrets in
# deploy/secrets.txt, so a deploy from a laptop, Cloud Shell or the merge trigger is identical.
set -euo pipefail
source "$(dirname "$0")/config.sh"
require_project
cd "$DEPLOY_DIR/.."

image=""
skip_tests=""
while [ $# -gt 0 ]; do
  case "$1" in
    --image) image="${2:?--image needs an image name}"; shift 2 ;;
    --skip-tests) skip_tests="yes"; shift ;;
    *) echo "Unknown option $1" >&2; exit 2 ;;
  esac
done

if [ -z "$image" ] && [ -z "$skip_tests" ]; then
  if python3 -c "import pytest, fastapi" 2>/dev/null; then
    echo "Running the tests first"
    python3 -m pytest -q
  else
    echo "pytest is not installed here; skipping the local test run (cloudbuild.yaml runs them)."
  fi
fi

# Non-secret settings: a temporary copy of grc-env-prod, deleted when the script ends.
env_file="$(mktemp)"
trap 'rm -f "$env_file"' EXIT
if ! env_text="$(gcloud secrets versions access latest --secret "$ENV_SECRET" \
  --project "$PROJECT")"; then
  echo "Cannot read $ENV_SECRET: fill deploy/env.prod.yaml, then deploy/setup_secrets.sh env" >&2
  exit 1
fi
# Empty values are left out: the settings default to empty anyway.
printf '%s\n' "$env_text" |
  grep -Ev '^[A-Z][A-Z0-9_]*:[[:space:]]*("")?[[:space:]]*(#.*)?$' >"$env_file" || true

# Secrets: every one in secrets.txt that has a value (WhatsApp ones may not exist yet).
secrets=""
while read -r var name; do
  if has_version "$name"; then
    secrets="${secrets:+$secrets,}${var}=${name}:latest"
  else
    echo "  $var not set: secret $name has no version yet (deploy/setup_secrets.sh $var)"
  fi
done < <(secret_lines)
secret_args=()
if [ -n "$secrets" ]; then
  secret_args=(--set-secrets "$secrets")
fi

if [ -n "$image" ]; then
  source_args=(--image "$image")
else
  source_args=(--source .)
fi

gcloud run deploy "$SERVICE" "${source_args[@]}" \
  --project "$PROJECT" --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --max-instances 1 --min-instances 0 \
  --no-cpu-throttling --memory 1Gi --timeout 300 \
  --allow-unauthenticated \
  --env-vars-file "$env_file" \
  ${secret_args[@]+"${secret_args[@]}"} \
  --quiet

# Google Chat and Meta cannot send Google tokens: the service is public and the app checks every
# caller itself (DESIGN section 9). --allow-unauthenticated can fail silently, so set it again.
gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --member allUsers --role roles/run.invoker --quiet >/dev/null

url="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format 'value(status.url)')"
service_url="$(yaml_value SERVICE_URL "$env_file")"
echo
echo "Deployed $SERVICE in $REGION"
echo "  Service URL:                 $url"
echo "  SERVICE_URL (env.prod.yaml): ${service_url:-NOT SET}"
echo "  WhatsApp webhook for Meta:   ${service_url:-$url}/wa/webhook"
echo "    (verify token: WA_VERIFY_TOKEN in deploy/env.prod.yaml)"
if command -v curl >/dev/null 2>&1; then
  health="$(curl -s --max-time 20 -w ' (HTTP %{http_code})' "${service_url:-$url}/health" |
    tr -d '\n' | tail -c 300 || true)"
  echo "  Health:                      $health"
fi
echo "Next (first time): deploy/setup_scheduler.sh"
