#!/usr/bin/env bash
# Convenience wrapper for running a live audit against a URL from the CLI —
# activates the venv and calls gtm_verifier/run.py, per HOWTORUN.txt.
#
# Usage:
#   ./run_audit.sh <url> [extra run.py args...]
#
# Examples:
#   ./run_audit.sh https://example.com
#   ./run_audit.sh https://example.com consent_audit
#   ./run_audit.sh https://example.com --export report.pptx
#   HEADLESS=1 ./run_audit.sh https://example.com   # no visible Chrome window

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <url> [extra run.py args...]" >&2
  echo "  e.g. $0 https://example.com" >&2
  echo "       HEADLESS=1 $0 https://example.com --export report.pptx" >&2
  exit 1
fi

URL="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/venv/bin/activate"
cd "$SCRIPT_DIR/gtm_verifier"
exec python run.py --url "$URL" "$@"
