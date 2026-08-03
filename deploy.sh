#!/usr/bin/env bash
# Deploy the web front end to Cloud Run.
#
# Builds from source (Cloud Build picks up the Dockerfile — no local Docker
# needed) and applies the service settings the app depends on. Re-run it any
# time to ship a new revision.
#
# Usage:
#   ./deploy.sh                     # deploy to the defaults below
#   SERVICE=gtm-verifier-staging ./deploy.sh
#   PROJECT_ID=other-project REGION=europe-west1 ./deploy.sh
#
# First-time setup (once per machine / project) is in HOWTORUN.txt.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-project-atlas-audit}"
REGION="${REGION:-europe-west2}"
SERVICE="${SERVICE:-gtm-verifier}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Deploying $SERVICE to $PROJECT_ID ($REGION)…"

# Flag notes — most of these stand in for code fixes still open in
# notes/cloud-followups.md, so don't drop them without reading it first:
#
#   --concurrency 1      config.py mutates module globals per request (#2), and
#                        network.LAST_RUN is shared the same way. One request
#                        per instance is what makes that safe; parallel users
#                        get separate instances, which are separate processes.
#   --session-affinity   the .pptx download reads an in-process cache (#3), so
#                        it has to land on the instance that ran the audit.
#   --timeout 3600       audits run inline and take 1-2 min, journeys longer (#1).
#   --memory 4Gi         Chromium needs >= 2 GB, and /tmp is tmpfs on top (#6).
#   --cpu-boost          cold start includes launching a browser.
#   gen2                 real /dev/shm and full syscall compatibility for Chromium.
#   --no-allow-unauthenticated + --ingress all
#                        correct pairing for IAP: IAP terminates auth in front
#                        while the service itself stays IAM-protected (#4).
gcloud run deploy "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --source . \
  --memory 4Gi \
  --cpu 2 \
  --cpu-boost \
  --concurrency 1 \
  --min-instances 0 \
  --max-instances 4 \
  --timeout 3600 \
  --session-affinity \
  --execution-environment gen2 \
  --ingress all \
  --no-allow-unauthenticated \
  --set-env-vars HEADLESS=1,PYTHONUNBUFFERED=1

SERVICE_URL="$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"

# Report the access model as it actually is, not as intended — the service is
# IAM-protected from the first deploy, but IAP is a separate opt-in step and
# until it's on, a browser gets a bare 403 with no sign-in prompt.
IAP_ENABLED="$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" \
  --format='value(metadata.annotations."run.googleapis.com/iap-enabled")' 2>/dev/null)"

echo
echo "Deployed: $SERVICE_URL"
echo
if [ "$IAP_ENABLED" = "True" ] || [ "$IAP_ENABLED" = "true" ]; then
  echo "IAP is on — open the URL and sign in with an account granted"
  echo "roles/iap.httpsResourceAccessor."
else
  echo "IAP is NOT enabled — the service only accepts a bearer token, so"
  echo "opening the URL in a browser returns 403. Either finish the IAP setup"
  echo "in HOWTORUN.txt, or reach it in a browser meanwhile with:"
  echo "  gcloud run services proxy $SERVICE --project $PROJECT_ID --region $REGION"
fi
echo
echo "Logs:"
echo "  gcloud run services logs read $SERVICE --project $PROJECT_ID --region $REGION"
