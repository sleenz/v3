# Names shared by the deploy scripts and cloudbuild.yaml (DESIGN section 16 item 9, section 17).
# Sourced by the other scripts; do not run it on its own. Change a name here and only here.

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="asia-southeast2"                 # Jakarta: the Gemini API is offered there (D17)
SERVICE="v2-git"                         # Cloud Run service
RUNTIME_SA_NAME="grc-tracker-run"        # the service runs as this account
SCHEDULER_SA_NAME="grc-tracker-scheduler"
REPO="cloud-run-source-deploy"           # Artifact Registry repository (Docker)
QUEUE="wa-process"                       # Cloud Tasks queue of the WhatsApp bot
GMAIL_TOPIC="grc-gmail-push"             # Pub/Sub topic: Gmail's new-mail notes (instant email)
GMAIL_SUBSCRIPTION="grc-gmail-push"      # push subscription that calls /jobs/gmail-push
ENV_SECRET="grc-env-prod"                # deploy/env.prod.yaml, stored whole as one secret

RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SCHEDULER_SA="${SCHEDULER_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_FILE="$DEPLOY_DIR/secrets.txt"

require_project() {
  if [ -z "$PROJECT" ]; then
    echo "No Google Cloud project selected. Run: gcloud config set project YOUR-PROJECT-ID" >&2
    exit 1
  fi
}

# "ENV_VAR SECRET_NAME" for every secret in deploy/secrets.txt.
secret_lines() {
  grep -Ev '^[[:space:]]*(#|$)' "$SECRETS_FILE" | awk '{print $1, $2}'
}

# The value of KEY in a flat YAML file such as env.prod.yaml (quotes removed).
yaml_value() {
  sed -nE "s/^$1:[[:space:]]*[\"']?([^\"'#]*[^\"'#[:space:]])?[\"']?[[:space:]]*(#.*)?$/\1/p" "$2" | head -n 1
}

ensure_secret() {
  gcloud secrets describe "$1" --project "$PROJECT" >/dev/null 2>&1 ||
    gcloud secrets create "$1" --replication-policy=automatic --project "$PROJECT" >/dev/null
}

has_version() {
  gcloud secrets versions list "$1" --project "$PROJECT" --filter="state=ENABLED" --limit=1 \
    --format="value(name)" 2>/dev/null | grep -q .
}
