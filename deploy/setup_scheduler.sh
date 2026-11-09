#!/usr/bin/env bash
# Create or update every Cloud Scheduler job (DESIGN section 7), all in Asia/Hong_Kong time, each
# calling the service with an OIDC token of the scheduler account (audience = SERVICE_URL).
#   deploy/setup_scheduler.sh
set -euo pipefail
source "$(dirname "$0")/config.sh"
require_project

env_file="$(mktemp)"
trap 'rm -f "$env_file"' EXIT
gcloud secrets versions access latest --secret "$ENV_SECRET" --project "$PROJECT" >"$env_file"
url="$(yaml_value SERVICE_URL "$env_file")"
if [ -z "$url" ]; then
  echo "SERVICE_URL is empty in $ENV_SECRET (deploy/env.prod.yaml)" >&2
  exit 1
fi

gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --member "serviceAccount:$SCHEDULER_SA" --role roles/run.invoker --quiet >/dev/null

# name | schedule | path. "*/5 8-19" runs 08:00-19:55. The WhatsApp reminder job runs every
# 30 minutes on weekdays and the app sends once, at the first run after Config "Reminder time".
jobs=(
  "grc-gmail|*/10 * * * *|/jobs/gmail"
  "grc-changes|*/5 8-19 * * *|/jobs/changes"
  "grc-recurring|0 7 * * *|/jobs/recurring"
  "grc-digest|0 9 * * 1-5|/jobs/digest"
  "grc-wa-reminders|*/30 7-19 * * 1-5|/jobs/wa-reminders"
  "grc-wa-drafts|*/10 * * * *|/jobs/wa-drafts"
  "grc-gmail-watch|23 6 * * *|/jobs/gmail-watch"
)
for job in "${jobs[@]}"; do
  IFS='|' read -r name schedule path <<<"$job"
  args=(
    --project "$PROJECT" --location "$REGION"
    --schedule "$schedule" --time-zone "Asia/Hong_Kong"
    --uri "$url$path" --http-method POST
    --oidc-service-account-email "$SCHEDULER_SA" --oidc-token-audience "$url"
    --attempt-deadline 300s --quiet
  )
  if gcloud scheduler jobs describe "$name" --project "$PROJECT" --location "$REGION" \
    >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$name" "${args[@]}" >/dev/null
    echo "updated $name ($schedule)"
  else
    gcloud scheduler jobs create http "$name" "${args[@]}" >/dev/null
    echo "created $name ($schedule)"
  fi
done

# Instant email (DESIGN section 20): Gmail -> topic -> this push subscription -> /jobs/gmail-push,
# with an OIDC token of the scheduler account (the app checks it like a Scheduler call).
topic="$(yaml_value GMAIL_PUSH_TOPIC "$env_file")"
if [ -z "$topic" ]; then
  echo "GMAIL_PUSH_TOPIC is empty: instant email is off; the 10-minute run reads the inbox."
  exit 0
fi
number="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
# Projects made before April 2021: Pub/Sub must be allowed to sign tokens as that account.
gcloud iam service-accounts add-iam-policy-binding "$SCHEDULER_SA" --project "$PROJECT" \
  --member "serviceAccount:service-$number@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role roles/iam.serviceAccountTokenCreator >/dev/null 2>&1 || true
push=(
  --project "$PROJECT" --push-endpoint "$url/jobs/gmail-push"
  --push-auth-service-account "$SCHEDULER_SA" --push-auth-token-audience "$url"
  --ack-deadline 60 --message-retention-duration 1h --expiration-period never
)
if gcloud pubsub subscriptions describe "$GMAIL_SUBSCRIPTION" --project "$PROJECT" \
  >/dev/null 2>&1; then
  gcloud pubsub subscriptions update "$GMAIL_SUBSCRIPTION" "${push[@]}" >/dev/null
  echo "updated push subscription $GMAIL_SUBSCRIPTION"
else
  gcloud pubsub subscriptions create "$GMAIL_SUBSCRIPTION" --topic "$topic" "${push[@]}" >/dev/null
  echo "created push subscription $GMAIL_SUBSCRIPTION"
fi
# Start the push now instead of at tomorrow's renewal.
gcloud scheduler jobs run grc-gmail-watch --project "$PROJECT" --location "$REGION"
echo "Gmail push started. Check the log for gmail_watch (working) or gmail_watch_failed."
