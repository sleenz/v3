#!/usr/bin/env bash
# Write deploy/env.prod.yaml for the selected project. Everything is filled in from the project
# itself except the Sheet ID, which it asks for (the whole sheet link is fine too). WhatsApp stays
# off (WA_* empty). Then store the file as the secret every deploy reads:
#   deploy/make_env.sh
#   deploy/setup_secrets.sh env
set -euo pipefail
source "$(dirname "$0")/config.sh"
require_project

file="$DEPLOY_DIR/env.prod.yaml"
if [ -e "$file" ]; then
  read -r -p "deploy/env.prod.yaml already exists. Overwrite it? [y/N] " answer
  case "$answer" in
    y | Y) ;;
    *) echo "Left unchanged." && exit 0 ;;
  esac
fi

number="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
if [ -z "$number" ]; then
  echo "Could not read the project number of $PROJECT." >&2
  exit 1
fi

read -r -p "Sheet ID or the sheet's whole link: " sheet
sheet="${sheet#*/d/}" # a pasted link: keep what follows /d/ ...
sheet="${sheet%%/*}"  # ... up to the next slash
sheet="${sheet%%\?*}"
sheet="${sheet%%#*}"
if ! [[ "$sheet" =~ ^[A-Za-z0-9_-]{20,}$ ]]; then
  echo "That does not look like a Sheet ID (letters, digits, - and _, from the sheet's link)." >&2
  exit 1
fi

cat >"$file" <<EOF
# Written by deploy/make_env.sh. Not secret. Store it with: deploy/setup_secrets.sh env
GCP_PROJECT: "$PROJECT"
GCP_REGION: "$REGION"
SHEET_ID: "$sheet"
SHARED_DRIVE_FOLDER_ID: ""
SERVICE_URL: "https://$SERVICE-$number.$REGION.run.app"
SCHEDULER_SA_EMAIL: "$SCHEDULER_SA"
RUNTIME_SA_EMAIL: "$RUNTIME_SA"
CHAT_PROJECT_NUMBER: "$number"
GMAIL_PUSH_TOPIC: "projects/$PROJECT/topics/$GMAIL_TOPIC"
WA_PHONE_NUMBER_ID: ""
WA_WABA_ID: ""
WA_GRAPH_VERSION: ""
WA_VERIFY_TOKEN: ""
WA_TASKS_QUEUE: "$QUEUE"
TZ: "Asia/Hong_Kong"
EOF
echo "Wrote deploy/env.prod.yaml for $PROJECT ($number)."
echo "Next: deploy/setup_secrets.sh env"
