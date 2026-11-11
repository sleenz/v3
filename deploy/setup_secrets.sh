#!/usr/bin/env bash
# Add a new version of the secrets in deploy/secrets.txt, and of deploy/env.prod.yaml.
#   deploy/setup_secrets.sh                  every secret, then env.prod.yaml
#   deploy/setup_secrets.sh WA_APP_SECRET    one secret (its ENV_VAR or its secret name)
#   deploy/setup_secrets.sh env              only deploy/env.prod.yaml
# Each value comes from a file you name or is typed at a hidden prompt. Values are never shown,
# never taken as command-line arguments and never written to disk. A new version takes effect
# at the next deploy (deploy/deploy.sh): Cloud Run reads secrets when a revision starts.
set -euo pipefail
source "$(dirname "$0")/config.sh"
require_project

add_secret() {
  local var="$1" name="$2" path value
  ensure_secret "$name"
  gcloud secrets add-iam-policy-binding "$name" --project "$PROJECT" \
    --member "serviceAccount:$RUNTIME_SA" --role roles/secretmanager.secretAccessor >/dev/null
  echo "$var (secret $name)"
  read -r -p "  Path to a file holding the value, Enter to type it hidden, s to skip: " path
  case "$path" in
    s | S)
      echo "  skipped"
      ;;
    "")
      read -r -s -p "  Value (hidden): " value
      echo
      if [ -z "$value" ]; then
        echo "  empty, skipped"
        return
      fi
      printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- \
        --project "$PROJECT" >/dev/null
      unset value
      echo "  new version added"
      ;;
    *)
      path="${path/#\~/$HOME}"
      if [ ! -f "$path" ]; then
        echo "  no file there, skipped"
        return
      fi
      gcloud secrets versions add "$name" --data-file="$path" --project "$PROJECT" >/dev/null
      echo "  new version added from the file"
      ;;
  esac
}

add_env() {
  local file="$DEPLOY_DIR/env.prod.yaml"
  if [ ! -f "$file" ]; then
    echo "No $file yet: copy deploy/env.prod.yaml.example to it and fill it in." >&2
    return 1
  fi
  ensure_secret "$ENV_SECRET"
  gcloud secrets versions add "$ENV_SECRET" --data-file="$file" --project "$PROJECT" >/dev/null
  echo "env.prod.yaml stored as $ENV_SECRET"
}

wanted="${1:-}"
if [ "$wanted" = "env" ]; then
  add_env
  exit 0
fi
found=""
while read -r var name; do
  if [ -z "$wanted" ] || [ "$wanted" = "$var" ] || [ "$wanted" = "$name" ]; then
    add_secret "$var" "$name" </dev/tty
    found="yes"
  fi
done < <(secret_lines)
if [ -n "$wanted" ] && [ -z "$found" ]; then
  echo "$wanted is not in deploy/secrets.txt" >&2
  exit 1
fi
if [ -z "$wanted" ]; then
  add_env
fi
