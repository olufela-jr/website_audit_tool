#!/usr/bin/env bash
# Manage who can INVOKE the Cloud Run service directly.
#
# Grants roles/run.invoker on the Cloud Run service itself. This is a different
# permission from the one in iap_access.sh, at a different layer:
#
#   browser user --[iap.httpsResourceAccessor]--> IAP --[run.invoker]--> service
#
# IAP is what invokes the service on a user's behalf, using its own service
# agent (service-<PROJECT_NUMBER>@gcp-sa-iap.iam.gserviceaccount.com). That
# agent is granted run.invoker automatically when IAP is enabled, and it is
# normally the ONLY holder of this role. People do not need it.
#
# So: to let a person use the tool, use iap_access.sh, not this script.
#
# What this is actually for:
#   * inspecting who/what can invoke the service (`list`)
#   * a service account calling the app programmatically
#   * restoring the IAP service agent's binding if it is ever removed
#
# IMPORTANT — granting run.invoker does NOT bypass IAP. While IAP is enabled it
# intercepts every external request, and a plain Cloud Run identity token is
# rejected with 401 (verified on this service). Calling through IAP needs an
# OIDC token minted for IAP's OAuth client ID as the audience, not the
# `gcloud auth print-identity-token` default. That path is not set up here.
#
# Usage:
#   ./invoker_access.sh list
#   ./invoker_access.sh add serviceAccount:robot@project.iam.gserviceaccount.com
#   ./invoker_access.sh remove serviceAccount:robot@project.iam.gserviceaccount.com

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-project-atlas-audit}"
REGION="${REGION:-europe-west2}"
SERVICE="${SERVICE:-gtm-verifier}"
ROLE="roles/run.invoker"

RUN_ARGS=(--project "$PROJECT_ID" --region "$REGION")

usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

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
    echo "Who/what can invoke $SERVICE ($ROLE):"
    echo "(the gcp-sa-iap service agent is expected here — that is IAP itself)"
    gcloud run services get-iam-policy "$SERVICE" "${RUN_ARGS[@]}"
    ;;
  add|remove)
    [ "$#" -gt 0 ] || { echo "error: give at least one member" >&2; exit 1; }
    for who in "$@"; do
      member="$(as_member "$who")"
      echo "--- ${ACTION}: ${member} ---"
      gcloud run services "${ACTION}-iam-policy-binding" "$SERVICE" "${RUN_ARGS[@]}" \
        --member="$member" --role="$ROLE"
    done
    echo
    echo "Note: this alone does not grant browser access — IAP still gates that."
    echo "To let a person use the tool, run: ./iap_access.sh add <email>"
    ;;
  *)
    usage
    ;;
esac
