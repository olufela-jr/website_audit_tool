"""
Analytics setup detection.

Establishes whether a valid GA4/GTM implementation exists and how it is deployed.
Uses DOM inspection, JavaScript object state, and resource timing — no network proxy.

Outputs per page load:
  - GA4 presence and measurement ID(s)
  - Duplicate / conflicting IDs
  - Deployment method: GTM | gtag.js | Hardcoded
  - First-party vs third-party routing
  - sGTM / GTG detection signals
"""

import re
import time
from typing import List
from urllib.parse import urlparse

from selenium.common.exceptions import WebDriverException

from core import CheckResult, accept_consent, make_driver

# ── Patterns ──────────────────────────────────────────────────────────────────
_RE_GA4_ID  = re.compile(r'\bG-[A-Z0-9]{6,}\b')
_RE_GTM_ID  = re.compile(r'\bGTM-[A-Z0-9]{4,}\b')

_RE_GA_HOST  = re.compile(r'(?:^|\.)google-analytics\.com$')
_RE_GTM_HOST = re.compile(r'(?:^|\.)googletagmanager\.com$')


def _exec(driver, expr: str):
    return driver.execute_script(f"return ({expr})")


def _host(url_str: str) -> str:
    try:
        return urlparse(url_str).hostname or ""
    except Exception:
        return ""


def _poll_resources(driver, match_fn, timeout: float = 8.0) -> List[str]:
    """Poll resource timing entries until match_fn hits or timeout. Returns matched URLs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        urls = _exec(driver, "performance.getEntriesByType('resource').map(e => e.name)") or []
        matched = [u for u in urls if match_fn(u)]
        if matched:
            return matched
        time.sleep(0.3)
    return []


def run_analytics_audit(url: str) -> List[CheckResult]:
    driver = make_driver()
    results = []
    try:
        driver.get(url)
        accept_consent(driver)

        # ── DOM: script tags ──────────────────────────────────────────────
        scripts = _exec(driver, """
            Array.from(document.querySelectorAll('script')).map(s => ({
                src:  s.src || '',
                text: (s.textContent || '').substring(0, 3000)
            }))
        """) or []

        # ── JavaScript objects ────────────────────────────────────────────
        js = _exec(driver, """({
            hasDataLayer : Array.isArray(window.dataLayer),
            hasGtag      : typeof window.gtag === 'function',
            hasGa        : typeof window.ga   === 'function',
            hasGTMObj    : typeof window.google_tag_manager === 'object'
                           && window.google_tag_manager !== null,
            gtmKeys      : Object.keys(window.google_tag_manager || {})
        })""") or {}

        # ── Resource timing (poll so async GA4 hits have time to fire) ────
        collect_urls = _poll_resources(
            driver,
            lambda u: "/collect" in u or "collect?" in u,
            timeout=6.0,
        )
        all_resources = (
            _exec(driver, "performance.getEntriesByType('resource').map(e => e.name)") or []
        )

        # ── Build corpus for pattern matching ─────────────────────────────
        corpus = " ".join(
            s.get("src", "") + " " + s.get("text", "") for s in scripts
        ) + " " + " ".join(all_resources)

        ga4_ids = sorted(set(_RE_GA4_ID.findall(corpus)))
        gtm_ids = sorted(set(_RE_GTM_ID.findall(corpus)))

        # Live GTM container keys are most authoritative
        live_containers = [k for k in js.get("gtmKeys", []) if k.startswith("GTM-")]
        if live_containers:
            gtm_ids = sorted(set(gtm_ids + live_containers))

        # ── Classify script sources ───────────────────────────────────────
        gtm_srcs  = [s["src"] for s in scripts if "gtm.js"  in s.get("src", "")]
        gtag_srcs = [s["src"] for s in scripts if "/gtag/js" in s.get("src", "")]
        inline_gtag = any("gtag(" in s.get("text", "") for s in scripts)

        fp_gtm_srcs  = [u for u in gtm_srcs  if not _RE_GTM_HOST.search(_host(u))]
        tp_gtm_srcs  = [u for u in gtm_srcs  if     _RE_GTM_HOST.search(_host(u))]
        fp_gtag_srcs = [u for u in gtag_srcs if not _RE_GTM_HOST.search(_host(u))]

        fp_collect = [u for u in collect_urls if not _RE_GA_HOST.search(_host(u))]
        tp_collect = [u for u in collect_urls if     _RE_GA_HOST.search(_host(u))]

        # ── Check: GA4 presence ───────────────────────────────────────────
        results.append(CheckResult(
            name="GA4 presence",
            event=None,
            passed=bool(ga4_ids or collect_urls),
            detail=(
                f"Measurement IDs detected: {', '.join(ga4_ids)}"
                if ga4_ids
                else "No GA4 measurement ID found in scripts or resource timing"
            ),
        ))

        # ── Check: duplicate measurement IDs ─────────────────────────────
        if len(ga4_ids) > 1:
            results.append(CheckResult(
                name="Single measurement ID",
                event=None,
                passed=False,
                detail=f"Multiple IDs — likely duplicate firing: {', '.join(ga4_ids)}",
            ))
        else:
            results.append(CheckResult(
                name="Single measurement ID",
                event=None,
                passed=True,
                detail=f"One ID: {ga4_ids[0]}" if ga4_ids else "No duplicates (none found)",
            ))

        # ── Check: deployment method ──────────────────────────────────────
        if gtm_srcs or js.get("hasGTMObj"):
            method = "GTM"
            method_detail = f"Containers: {', '.join(gtm_ids) or '(ID not resolved)'}"
        elif gtag_srcs or (js.get("hasGtag") and not js.get("hasGTMObj")):
            method = "gtag.js"
            method_detail = "gtag.js loader present (no GTM)"
        elif inline_gtag:
            method = "Hardcoded gtag"
            method_detail = "gtag() found in inline <script> — no external loader"
        else:
            method = None
            method_detail = "No recognised tag deployment detected"

        results.append(CheckResult(
            name="Deployment method",
            event=None,
            passed=method is not None,
            detail=f"{method} — {method_detail}" if method else method_detail,
        ))

        # ── Check: conflicting implementations ────────────────────────────
        # GTM + standalone gtag.js together = high duplicate-firing risk
        if tp_gtm_srcs and gtag_srcs:
            results.append(CheckResult(
                name="No conflicting implementations",
                event=None,
                passed=False,
                detail="GTM and standalone gtag.js both loaded — high risk of duplicate GA4 hits",
            ))
        else:
            results.append(CheckResult(
                name="No conflicting implementations",
                event=None,
                passed=True,
                detail="Single deployment pathway confirmed",
            ))

        # ── Check: first-party vs third-party ────────────────────────────
        fp_signals = fp_collect + fp_gtm_srcs + fp_gtag_srcs
        tp_signals = tp_collect + tp_gtm_srcs + gtag_srcs

        if fp_signals:
            results.append(CheckResult(
                name="First-party tagging",
                event=None,
                passed=True,
                detail=f"First-party endpoints in use: {', '.join(fp_signals[:2])}",
            ))
        elif tp_signals:
            results.append(CheckResult(
                name="First-party tagging",
                event=None,
                passed=False,
                detail="Third-party only — routed directly to google-analytics.com / googletagmanager.com",
            ))
        else:
            results.append(CheckResult(
                name="First-party tagging",
                event=None,
                passed=False,
                detail=(
                    "No collect requests observed in resource timing "
                    "(may fire via sendBeacon after user interaction)"
                ),
            ))

        # ── Check: sGTM / GTG signals ─────────────────────────────────────
        sgtm_signals = []
        if fp_gtm_srcs:
            sgtm_signals.append(f"GTM served from custom domain: {fp_gtm_srcs[0]}")
        if fp_collect:
            sgtm_signals.append(f"Collect endpoint on custom domain: {fp_collect[0]}")
        gtg_hits = [u for u in all_resources if "/gtg" in u or "gtg.json" in u]
        if gtg_hits:
            sgtm_signals.append(f"GTG endpoint: {gtg_hits[0]}")

        results.append(CheckResult(
            name="sGTM / GTG signals",
            event=None,
            passed=bool(sgtm_signals),
            detail=(
                "; ".join(sgtm_signals)
                if sgtm_signals
                else "No server-side tagging signals detected"
            ),
        ))

    except WebDriverException as exc:
        results.append(CheckResult(
            name="analytics_audit",
            event=None,
            passed=False,
            detail=f"Driver error during audit: {exc}",
        ))
    finally:
        driver.quit()
    return results
