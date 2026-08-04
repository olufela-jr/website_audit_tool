#!/usr/bin/env bash
# Manage who can sign in to the deployed audit tool.
#
# Authentication is Google OAuth (gtm_verifier/webapp/auth.py); this allowlist
# is the authorisation. A valid Google account that is not on the list gets a
# "you don't have access" page.
#
# The list lives in the ALLOWED_USERS environment variable on the Cloud Run
# service, so changes here update the service in place — a new revision, but no
# rebuild, so it takes about half a minute. Keep deploy.sh's ALLOWED_USERS
# default in sync, or the next full deploy will overwrite what you set here.
#
# Usage:
#   ./users.sh list
#   ./users.sh add someone@example.com [another@example.com ...]
#   ./users.sh remove someone@example.com
#
# Any Google account works — gmail.com, a client's domain, anything. That was
# the whole reason for moving off IAP, whose Google-managed OAuth client only
# admitted identities inside this project's organisation.
#
# Everyone on this list can drive a real browser at any public URL from inside
# GCP. Private/loopback/link-local targets are refused (gtm_verifier/net_guard.py),
# but read notes/cloud-followups.md #4 before adding people outside the team.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-project-atlas-audit}"
REGION="${REGION:-europe-west2}"
SERVICE="${SERVICE:-gtm-verifier}"

RUN_ARGS=(--project "$PROJECT_ID" --region "$REGION")

usage() { sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

current_list() {
  gcloud run services describe "$SERVICE" "${RUN_ARGS[@]}" \
    --format='value(spec.template.spec.containers[0].env.filter("name:ALLOWED_USERS").extract("value"))' \
    2>/dev/null | tr -d '[]' | tr ',' '\n' | sed 's/^ *//;s/ *$//' | sed '/^$/d'
}

apply_list() {
  # Comma-separated, no spaces. gcloud splits --update-env-vars on commas, so
  # the value is passed with a custom delimiter to keep it as one variable.
  # The delimiter must be ';' — '@' would seem natural but appears in every
  # email address, and gcloud would split the list right back apart.
  local joined
  joined="$(paste -sd, -)"
  if [ -z "$joined" ]; then
    echo "error: refusing to set an empty allowlist — nobody could sign in." >&2
    echo "       (the app also fails to start with ALLOWED_USERS empty)" >&2
    exit 1
  fi
  echo "New allowlist: $joined"
  gcloud run services update "$SERVICE" "${RUN_ARGS[@]}" \
    --update-env-vars "^;^ALLOWED_USERS=${joined}"
  echo
  echo "Done. Remember to update ALLOWED_USERS in deploy.sh to match, or the"
  echo "next ./deploy.sh will revert this."
}

ACTION="${1:-}"
[ -n "$ACTION" ] || usage
shift || true

case "$ACTION" in
  list)
    echo "Who can sign in to $SERVICE:"
    current_list | sed 's/^/  /'
    ;;
  add)
    [ "$#" -gt 0 ] || { echo "error: give at least one email address" >&2; exit 1; }
    { current_list; printf '%s\n' "$@"; } \
      | tr '[:upper:]' '[:lower:]' | sed '/^$/d' | sort -u | apply_list
    ;;
  remove)
    [ "$#" -gt 0 ] || { echo "error: give at least one email address" >&2; exit 1; }
    keep="$(current_list)"
    for who in "$@"; do
      keep="$(printf '%s\n' "$keep" | grep -vixF "$who" || true)"
    done
    printf '%s\n' "$keep" | sed '/^$/d' | apply_list
    ;;
  *)
    usage
    ;;
esac
