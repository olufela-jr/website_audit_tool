#!/usr/bin/env bash
# Manage who can USE the audit tool in a browser.
#
# Grants roles/iap.httpsResourceAccessor on the IAP resource in front of the
# Cloud Run service. This is the permission a person needs — the only one.
# Without it they reach Google sign-in, authenticate fine, and are then stopped
# by IAP with "You don't have access" before a single request touches the app.
#
# Any domain works; the org has no domain-restricted-sharing policy.
#
# Usage:
#   ./iap_access.sh list
#   ./iap_access.sh add someone@example.com [another@example.com ...]
#   ./iap_access.sh remove someone@example.com
#
# For a group instead of individuals (better past a handful of people):
#   ./iap_access.sh add group:audit-team@cloud-runner.co.uk
#
# See invoker_access.sh for the OTHER permission, and the header there for how
# the two fit together.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-project-atlas-audit}"
REGION="${REGION:-europe-west2}"
SERVICE="${SERVICE:-gtm-verifier}"
ROLE="roles/iap.httpsResourceAccessor"

# The binding lives on the IAP resource, NOT the Cloud Run service --
# `gcloud run services add-iam-policy-binding` rejects this role outright with
# "not supported for this resource". Plain `gcloud iap web` has no cloud-run
# resource type either, so `beta` is required here.
IAP_ARGS=(
  --project "$PROJECT_ID"
  --resource-type=cloud-run
  --service="$SERVICE"
  --region="$REGION"
)

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

# Bare addresses are the common case; accept an explicit `group:` / `user:` /
# `serviceAccount:` prefix too and pass it through untouched.
as_member() {
  case "$1" in
    user:*|group:*|serviceAccount:*|domain:*) printf '%s' "$1" ;;
    *) printf 'user:%s' "$1" ;;
  esac
}

ACTION="${1:-}"
[ -n "$ACTION" ] || usage
shift || true

case "$ACTION" in
  list)
    echo "Who can use $SERVICE in a browser ($ROLE):"
    gcloud beta iap web get-iam-policy "${IAP_ARGS[@]}"
    ;;
  add|remove)
    [ "$#" -gt 0 ] || { echo "error: give at least one email address" >&2; exit 1; }
    for who in "$@"; do
      member="$(as_member "$who")"
      echo "--- ${ACTION}: ${member} ---"
      gcloud beta iap web "${ACTION}-iam-policy-binding" "${IAP_ARGS[@]}" \
        --member="$member" --role="$ROLE"
    done
    echo
    echo "Done. Changes take up to a minute; a signed-in user may need a hard"
    echo "reload, or the 'try a different account' link, to clear the old verdict."
    ;;
  *)
    usage
    ;;
esac
