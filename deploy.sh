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

# Who may sign in. Authentication is Google OAuth (webapp/auth.py); this list is
# the authorisation. Edit it and redeploy to add or remove someone.
#
# Not IAP: Cloud Run's built-in IAP authenticates against a Google-managed OAuth
# client that only admits identities inside the project's own organisation, so
# approved users on other domains could never get in. See notes/cloud-followups.md #4.
ALLOWED_USERS="${ALLOWED_USERS:-fela@cloud-runner.co.uk,misterfela@gmail.com,keith.lavender@assemblyglobal.com}"

# Secret Manager secret names (created once — see HOWTORUN.txt).
SECRET_FLASK_KEY="${SECRET_FLASK_KEY:-gtm-verifier-flask-key}"
SECRET_OAUTH_ID="${SECRET_OAUTH_ID:-gtm-verifier-oauth-client-id}"
SECRET_OAUTH_SECRET="${SECRET_OAUTH_SECRET:-gtm-verifier-oauth-client-secret}"

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
#   --allow-unauthenticated
#                        the app authenticates its own users (webapp/auth.py),
#                        so Cloud Run must let the sign-in page through. The
#                        service refuses to start if auth is unconfigured, so
#                        this cannot silently expose an open app.
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
  --allow-unauthenticated \
  `# ^;^ switches gcloud's delimiter to ';' so ALLOWED_USERS can hold a` \
  `# comma-separated list. Not '@' — that appears in every email address.` \
  --set-env-vars "^;^HEADLESS=1;PYTHONUNBUFFERED=1;ALLOWED_USERS=${ALLOWED_USERS}" \
  --set-secrets "FLASK_SECRET_KEY=${SECRET_FLASK_KEY}:latest,OAUTH_CLIENT_ID=${SECRET_OAUTH_ID}:latest,OAUTH_CLIENT_SECRET=${SECRET_OAUTH_SECRET}:latest"

SERVICE_URL="$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"

echo
echo "Deployed: $SERVICE_URL"
echo
echo "Sign-in is Google OAuth, allowlisted to:"
printf '  %s\n' "${ALLOWED_USERS//,/ }" | tr ' ' '\n' | sed '/^$/d;s/^/  /'
echo
echo "The OAuth client's authorised redirect URI must be exactly:"
echo "  ${SERVICE_URL}/auth/callback"
echo
echo "Logs:"
echo "  gcloud run services logs read $SERVICE --project $PROJECT_ID --region $REGION"
