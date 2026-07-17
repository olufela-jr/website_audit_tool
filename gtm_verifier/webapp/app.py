"""
Tiny local web front end for the GTM / GA4 verifier.

Audits are split into two streams by what they DO to the target site:

  • Passive     — observation only: load the page like any visitor and watch
                  what the site does on its own (tags, SEO, security headers,
                  analytics, consent, network). Safe on any public URL,
                  including prospects we have no relationship with.
  • Interactive — dataLayer journeys from an uploaded site config. These drive
                  the site: click funnels, submit forms with test data,
                  potentially place test orders — creating state on the site
                  and firing events into the client's production GA4. They only
                  run when the operator confirms client-granted authorization;
                  otherwise they are skipped as "access required".

Enter a URL, pick audits, see an HTML report, download the PowerPoint.

Run from the gtm_verifier directory:
    flask --app webapp.app run
    # or, to run Chrome silently:
    HEADLESS=1 flask --app webapp.app run

Then open http://localhost:5000.
"""

import io
import os
import re
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime
from typing import Dict, List, Tuple
from urllib.parse import urlparse

import yaml
from flask import Flask, abort, redirect, render_template, request, send_file, url_for

# The core audit modules (analytics.py, core.py, …) live one directory up and
# import each other as top-level modules (e.g. `import config`). Make sure that
# directory is importable no matter where the app is launched from.
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

import analytics  # noqa: E402
import browser  # noqa: E402
import config  # noqa: E402
import consent  # noqa: E402
import journeys as journeys_mod  # noqa: E402
import network  # noqa: E402
import remote_config  # noqa: E402
import seo  # noqa: E402
import security_headers  # noqa: E402
import tags_inventory  # noqa: E402
from core import CheckResult, Severity, failed_check, score_checks, skip_check  # noqa: E402
from export import export_to_powerpoint  # noqa: E402

app = Flask(__name__)

# Form field the operator ticks to confirm the client has authorized us to
# drive their site (journeys submit forms and can place test orders). Honour
# system — the tool does not verify it.
ACCESS_FIELD = "authorized"

# Audits split into two streams by what they do to the target site. Each item
# is (key, label, fn) where fn takes a URL and returns List[CheckResult].
# `requires_access` marks the Interactive stream: gated behind ACCESS_FIELD.
AUDIT_GROUPS = [
    {
        "id": "passive",
        "title": "Passive audit",
        "blurb": "Observation only — loads the page like a normal visitor and watches what "
                 "the site does. Safe to run on any public URL, prospects included.",
        "default": True,
        "requires_access": False,
        "items": [
            ("analytics", "Analytics setup", analytics.run_analytics_audit),
            ("consent", "Consent & privacy", consent.run_consent_audit),
            ("network", "Network / collect", network.run_network_audit),
            ("ga4_config", "GA4 property settings (remote config)", remote_config.run_remote_config_audit),
            ("tags", "Tag & pixel inventory", tags_inventory.run_tag_inventory_audit),
            ("seo", "SEO & metadata", seo.run_seo_audit),
            ("security", "Security headers", security_headers.run_security_headers_audit),
        ],
    },
    {
        "id": "interactive",
        "title": "Interactive audit",
        "blurb": "Drives the site: dataLayer journeys click through funnels, submit forms "
                 "with test data and can place test orders — creating state on the site and "
                 "firing events into the client's production analytics. Client permission "
                 "required; tick below to confirm and enable.",
        "default": False,
        "requires_access": True,
        "items": [],
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
    # Optional site config upload (YAML, see config.example.yaml): supplies the
    # CMP accept selector, expected GTM/GA4 IDs, and declarative journeys for
    # the target site — so new client sites need no code changes here either.
    site_config = None
    upload = request.files.get("config_file")
    if upload and upload.filename:
        try:
            site_config = yaml.safe_load(upload.read().decode("utf-8", errors="replace"))
            if not isinstance(site_config, dict):
                raise ValueError("top level must be a YAML mapping")
        except Exception as exc:
            return render_template(
                "index.html", groups=AUDIT_GROUPS,
                error=f"Could not parse the config file: {exc}",
            )

    url = (request.form.get("url") or "").strip()
    if not url and site_config:
        url = ((site_config.get("site") or {}).get("base_url") or "").strip()
    measurement_id = (request.form.get("measurement_id") or "").strip()

    selected = [name for name in AUDITS if request.form.get(name)]
    run_journeys = bool(request.form.get("journeys"))

    if not url:
        # A bare measurement ID can serve the ga4_config audit (pure HTTP);
        # every other audit needs a page to load.
        others = [n for n in selected if n != "ga4_config"] or (["journeys"] if run_journeys else [])
        if not measurement_id or others:
            return render_template(
                "index.html", groups=AUDIT_GROUPS, measurement_id=measurement_id,
                error=(
                    "Please enter a URL (or upload a config with site.base_url)."
                    if not measurement_id else
                    "A URL is required for the other selected audits — with only a "
                    "measurement ID, just 'GA4 property settings' can run."
                ),
            )
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Apply the uploaded config so the consent selector / timeouts take effect
    # for this run. Without an upload, whatever config.yaml auto-loaded stays —
    # same behaviour as the CLI. (Global state: fine for a single local process.)
    if site_config is not None:
        config.load_dict(site_config, path=upload.filename)
    if url:
        config.set_base_url(url)
    site_journeys = (site_config or {}).get("journeys") or {}

    if not selected and not run_journeys:
        return render_template(
            "index.html", groups=AUDIT_GROUPS, error="Pick at least one audit to run.", url=url
        )

    authorized = bool(request.form.get(ACCESS_FIELD))

    # One shared browser process for the whole request; each audit/journey
    # still gets a fresh isolated context (browser.audit_page). The session
    # lives entirely on this request thread — Playwright's sync API is
    # thread-affine, so it must never be shared across requests.
    journey_results: List[Tuple[str, List[CheckResult]]] = []

    def _run_selected():
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
                if name == "ga4_config":
                    # Form-supplied measurement ID wins; the uploaded config's
                    # expected ga4_id seeds the lookup; else IDs come from the page.
                    checks = remote_config.run_remote_config_audit(
                        url or None,
                        measurement_id=measurement_id or config.GA4_ID,
                        expected=config.ga4_expectations(),
                    )
                elif name == "analytics" and site_config is not None:
                    # Verify the uploaded config's expected tag IDs against the site
                    checks = analytics.run_analytics_audit(
                        url, expected_gtm_id=config.GTM_ID, expected_ga4_id=config.GA4_ID
                    )
                else:
                    checks = fn(url)
            except Exception as exc:  # one bad audit shouldn't 500 the whole page
                tb = traceback.format_exc()
                checks = [failed_check(name, f"Audit crashed: {exc}\n{tb}")]
            journey_results.append((name, checks))

    if url:
        with browser.browser_session():
            _run_selected()
            _run_config_journeys(journey_results, run_journeys, authorized, site_journeys)
    else:
        # ID-only run: ga4_config is pure HTTP — no browser needed.
        _run_selected()

    _prune_cache()
    run_id = uuid.uuid4().hex
    target = url or f"GA4 property {measurement_id}"
    _RUNS[run_id] = {"url": target, "results": journey_results, "created": time.time()}

    view = _build_view(journey_results)
    return render_template("results.html", url=target, run_id=run_id, **view)


def _run_config_journeys(journey_results, run_journeys, authorized, site_journeys):
    """dataLayer journeys from the uploaded config (Authorized stream)."""
    if not run_journeys:
        return
    if not authorized:
        journey_results.append(("journeys", [skip_check(
            "Journey verification",
            "Not assessed — access required: confirm authorized access to run journeys.",
            Severity.HIGH,
        )]))
    elif not site_journeys:
        journey_results.append(("journeys", [skip_check(
            "Journey verification",
            "No config uploaded (or it has no 'journeys:' section) — "
            "upload a site config YAML to run dataLayer journeys.",
            Severity.HIGH,
        )]))
    else:
        for jname, spec in site_journeys.items():
            try:
                checks = journeys_mod.run_journey(jname, spec)
            except Exception as exc:
                tb = traceback.format_exc()
                checks = [failed_check(jname, f"Journey crashed: {exc}\n{tb}")]
            journey_results.append((jname, checks))


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
        download_name=_report_filename(run),
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


def _report_filename(run: dict) -> str:
    """`<site>_<audit-date>.pptx` — e.g. tails.com_2026-07-17.pptx. The target
    may also be 'GA4 property G-XXXX' (ID-only runs), which has no hostname."""
    target = run["url"]
    site = urlparse(target).hostname or target
    site = re.sub(r"[^A-Za-z0-9.-]+", "_", site).strip("_.") or "audit"
    stamp = datetime.fromtimestamp(run["created"]).strftime("%Y-%m-%d")
    return f"{site}_{stamp}.pptx"


if __name__ == "__main__":
    app.run(debug=True)
