#!/usr/bin/env bash
# One-time Google Cloud setup. Safe to run again: it only creates what is missing.
#   deploy/setup_gcp.sh
# Enables the APIs, creates the two service accounts with only the roles they need, the empty
# secrets (deploy/secrets.txt and grc-env-prod), the Artifact Registry repository and the Cloud
# Tasks queue. Values go in afterwards with deploy/setup_secrets.sh.
set -euo pipefail
source "$(dirname "$0")/config.sh"
require_project

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
# The account Cloud Build runs as (also for `gcloud run deploy --source`). Override if your
# project uses another one: BUILD_SA=... deploy/setup_gcp.sh
BUILD_SA="${BUILD_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"

echo "Project $PROJECT ($PROJECT_NUMBER), region $REGION"

echo "1/7 APIs"
gcloud services enable --project "$PROJECT" \
  run.googleapis.com cloudscheduler.googleapis.com cloudtasks.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com \
  sheets.googleapis.com drive.googleapis.com gmail.googleapis.com chat.googleapis.com \
  iam.googleapis.com pubsub.googleapis.com

echo "2/7 Service accounts"
ensure_sa() {
  gcloud iam service-accounts describe "$1@$PROJECT.iam.gserviceaccount.com" \
    --project "$PROJECT" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$1" --display-name "$2" --project "$PROJECT"
}
ensure_sa "$RUNTIME_SA_NAME" "GRC tracker runtime"
ensure_sa "$SCHEDULER_SA_NAME" "GRC tracker scheduler"

echo "3/7 Secrets (empty until setup_secrets.sh) and who may read them"
bind_secret() {
  gcloud secrets add-iam-policy-binding "$1" --project "$PROJECT" \
    --member "serviceAccount:$2" --role "$3" >/dev/null
}
while read -r var name; do
  ensure_secret "$name"
  bind_secret "$name" "$RUNTIME_SA" roles/secretmanager.secretAccessor
  # The build account only sees that a version exists (deploy.sh skips empty secrets), not values.
  bind_secret "$name" "$BUILD_SA" roles/secretmanager.viewer
  echo "  $name -> $var (readable by $RUNTIME_SA_NAME)"
done < <(secret_lines)
ensure_secret "$ENV_SECRET"
bind_secret "$ENV_SECRET" "$BUILD_SA" roles/secretmanager.secretAccessor
bind_secret "$ENV_SECRET" "$BUILD_SA" roles/secretmanager.viewer
echo "  $ENV_SECRET (readable by the build account, which deploys with it)"

echo "4/7 Roles"
bind_project() {
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$1" --role "$2" \
    --condition=None >/dev/null
}
# Runtime: queue WhatsApp tasks, and sign those tasks' tokens as itself.
bind_project "$RUNTIME_SA" roles/cloudtasks.enqueuer
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" --project "$PROJECT" \
  --member "serviceAccount:$RUNTIME_SA" --role roles/iam.serviceAccountUser >/dev/null
# Build account: build, push and deploy the service as the runtime account.
for role in roles/run.admin roles/artifactregistry.writer roles/logging.logWriter \
  roles/storage.objectViewer; do
  bind_project "$BUILD_SA" "$role"
done
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" --project "$PROJECT" \
  --member "serviceAccount:$BUILD_SA" --role roles/iam.serviceAccountUser >/dev/null
# The scheduler account gets run.invoker on the service in setup_scheduler.sh (after deploy).

echo "5/7 Artifact Registry repository $REPO"
gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT" \
  >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$REPO" --repository-format=docker \
    --location "$REGION" --project "$PROJECT"

echo "6/7 Cloud Tasks queue $QUEUE"
gcloud tasks queues describe "$QUEUE" --location "$REGION" --project "$PROJECT" \
  >/dev/null 2>&1 ||
  gcloud tasks queues create "$QUEUE" --location "$REGION" --project "$PROJECT" \
    --max-attempts=10 --min-backoff=10s --max-backoff=300s

echo "7/7 Pub/Sub topic $GMAIL_TOPIC (instant email: Gmail announces new mail)"
gcloud pubsub topics describe "$GMAIL_TOPIC" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud pubsub topics create "$GMAIL_TOPIC" --project "$PROJECT" >/dev/null
if ! gcloud pubsub topics add-iam-policy-binding "$GMAIL_TOPIC" --project "$PROJECT" \
  --member serviceAccount:gmail-api-push@system.gserviceaccount.com \
  --role roles/pubsub.publisher >/dev/null; then
  echo "  WARNING: Gmail may not publish to $GMAIL_TOPIC. This is usually the organisation" >&2
  echo "  policy 'Domain restricted sharing': ask IT for an exception for" >&2
  echo "  gmail-api-push@system.gserviceaccount.com. Until then email is read every 10 minutes." >&2
fi
echo "Done. Next: deploy/setup_secrets.sh"
