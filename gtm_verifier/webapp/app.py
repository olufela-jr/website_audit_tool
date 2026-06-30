"""
Tiny local web front end for the GTM / GA4 verifier.

Audits are split into two streams by what access they require:

  • Public    — outside-in checks on any public URL, no affiliation or access
                needed (tags, analytics, consent, network). All browser-based.
  • Authorized — checks that need client-granted access: a server fetch from an
                allow-listed host, or GTM container access (security headers,
                SEO). These only run when the operator confirms authorized
                access; otherwise they are skipped as "access required".

Enter a URL, pick audits, see an HTML report, download the PowerPoint.

Run from the gtm_verifier directory:
    flask --app webapp.app run
    # or, to run Chrome silently:
    HEADLESS=1 flask --app webapp.app run

Then open http://localhost:5000.
"""

import io
import os
import sys
import tempfile
import time
import traceback
import uuid
from typing import Dict, List, Tuple

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

# The core audit modules (analytics.py, core.py, …) live one directory up and
# import each other as top-level modules (e.g. `import config`). Make sure that
# directory is importable no matter where the app is launched from.
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

import analytics  # noqa: E402
import consent  # noqa: E402
import network  # noqa: E402
import seo  # noqa: E402
import security_headers  # noqa: E402
import tags_inventory  # noqa: E402
from core import CheckResult, Severity, failed_check, score_checks, skip_check  # noqa: E402
from export import export_to_powerpoint  # noqa: E402

app = Flask(__name__)

# Form field the operator ticks to confirm they have authorized access to the
# target (allow-listed host / GTM container access). Honour system — the tool
# does not verify it; if access isn't really there the fetch just fails and the
# audit skips as "access required".
ACCESS_FIELD = "authorized"

# Audits split into two streams by the access they require. Each item is
# (key, label, fn) where fn takes a URL and returns List[CheckResult].
# `requires_access` marks the Authorized stream: gated behind ACCESS_FIELD.
AUDIT_GROUPS = [
    {
        "id": "public",
        "title": "Public audit",
        "blurb": "Outside-in checks anyone can run on a public URL — no affiliation or access required.",
        "default": True,
        "requires_access": False,
        "items": [
            ("tags", "Tag & pixel inventory", tags_inventory.run_tag_inventory_audit),
            ("seo", "SEO & metadata", seo.run_seo_audit),
        ],
    },
    {
        "id": "authorized",
        "title": "Authorized audit",
        "blurb": "Implementation, consent, network and security checks — shown only with "
                 "client-granted access. Tick below to confirm access and enable them.",
        "default": False,
        "requires_access": True,
        "items": [
            ("analytics", "Analytics setup", analytics.run_analytics_audit),
            ("consent", "Consent & privacy", consent.run_consent_audit),
            ("network", "Network / collect", network.run_network_audit),
            ("security", "Security headers", security_headers.run_security_headers_audit),
        ],
    },
]

# Flat lookup: checkbox key -> (display label, audit function).
AUDITS: Dict[str, Tuple[str, callable]] = {
    key: (label, fn) for g in AUDIT_GROUPS for key, label, fn in g["items"]
}

# Keys that belong to the Authorized stream (gated behind ACCESS_FIELD).
_AUTHORIZED_KEYS = {key for g in AUDIT_GROUPS if g["requires_access"] for key, _l, _fn in g["items"]}

# In-process cache of completed runs so the results page can offer a PowerPoint
# download without re-running the (slow) browser audits. Fine for a single local
# process; entries expire after _CACHE_TTL seconds.
_CACHE_TTL = 30 * 60
_RUNS: Dict[str, dict] = {}


def _prune_cache() -> None:
    cutoff = time.time() - _CACHE_TTL
    for run_id in [k for k, v in _RUNS.items() if v["created"] < cutoff]:
        _RUNS.pop(run_id, None)


def _status(result: CheckResult) -> str:
    """Collapse a CheckResult into a single display status (mirrors print_report)."""
    if result.skipped:
        return "SKIP"
    if result.severity == Severity.INFO:
        return "INFO"
    return "PASS" if result.passed else "FAIL"


def _build_view(journey_results: List[Tuple[str, List[CheckResult]]]) -> dict:
    """Shape audit results into a plain structure the template can iterate over."""
    audits = []
    overall_passed = overall_total = 0
    for name, checks in journey_results:
        passed, total, pct = score_checks(checks)
        overall_passed += passed
        overall_total += total
        # Split into scored CHECKS (PASS/FAIL/SKIP) and INFO OBSERVATIONS so the
        # UI never mixes graded judgements with neutral context.
        checks_rows, observations = [], []
        for c in checks:
            row = {
                "status": _status(c),
                "name": c.name,
                "severity": c.severity.value,
                "detail": c.detail,
                "event": c.event,
            }
            (observations if row["status"] == "INFO" else checks_rows).append(row)
        audits.append({
            "name": AUDITS.get(name, (name,))[0],
            "passed": passed,
            "total": total,
            "pct": pct,
            "checks": checks_rows,
            "observations": observations,
        })
    overall_pct = (overall_passed / overall_total * 100.0) if overall_total else 0.0
    return {
        "audits": audits,
        "overall_passed": overall_passed,
        "overall_total": overall_total,
        "overall_pct": overall_pct,
    }


@app.route("/")
def index():
    return render_template("index.html", groups=AUDIT_GROUPS)


@app.route("/audit", methods=["POST"])
def run_audit():
    url = (request.form.get("url") or "").strip()
    if not url:
        return render_template("index.html", groups=AUDIT_GROUPS, error="Please enter a URL.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    selected = [name for name in AUDITS if request.form.get(name)]
    if not selected:
        return render_template(
            "index.html", groups=AUDIT_GROUPS, error="Pick at least one audit to run.", url=url
        )

    authorized = bool(request.form.get(ACCESS_FIELD))

    journey_results: List[Tuple[str, List[CheckResult]]] = []
    for name in selected:
        label, fn = AUDITS[name]
        # Authorized-stream audits only run when the operator confirms access.
        # Without it they're skipped (score-neutral), never failed.
        if name in _AUTHORIZED_KEYS and not authorized:
            checks = [skip_check(
                label,
                "Not assessed — access required: confirm authorized access to run this audit.",
                Severity.HIGH,
            )]
            journey_results.append((name, checks))
            continue
        try:
            checks = fn(url)
        except Exception as exc:  # one bad audit shouldn't 500 the whole page
            tb = traceback.format_exc()
            checks = [failed_check(name, f"Audit crashed: {exc}\n{tb}")]
        journey_results.append((name, checks))

    _prune_cache()
    run_id = uuid.uuid4().hex
    _RUNS[run_id] = {"url": url, "results": journey_results, "created": time.time()}

    view = _build_view(journey_results)
    return render_template("results.html", url=url, run_id=run_id, **view)


@app.route("/audit/<run_id>/report.pptx")
def download_report(run_id: str):
    run = _RUNS.get(run_id)
    if not run:
        abort(404, "This report has expired — please re-run the audit.")
    # export_to_powerpoint writes to a path; generate into a temp file, read the
    # bytes back, and delete it immediately so nothing is left in /tmp.
    tmp = tempfile.NamedTemporaryFile(suffix=".pptx", delete=False)
    tmp.close()
    try:
        export_to_powerpoint(run["results"], run["url"], tmp.name)
        with open(tmp.name, "rb") as f:
            data = io.BytesIO(f.read())
    finally:
        os.unlink(tmp.name)
    data.seek(0)
    return send_file(
        data,
        as_attachment=True,
        download_name="gtm_audit.pptx",
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


if __name__ == "__main__":
    app.run(debug=True)
