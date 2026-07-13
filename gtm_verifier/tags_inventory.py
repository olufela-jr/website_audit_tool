"""
Tag & pixel inventory — publicly observable, no site access required.

Loads the page in a browser, accepts consent, and records every network request
URL (via Playwright request events) plus a set of known JavaScript globals. Each
is matched against a signature table of common analytics / advertising vendors
so the report can list "here is everything firing on your site".

The per-vendor list is INFO severity (an inventory, not pass/fail). On top of
that, two SCORED checks assert that GTM and GA4 are actually *running* — i.e.
at least one container / measurement ID is firing — and report how many. This
is a public, outside-in audit: there's no "expected" ID to match against (you
have no affiliation with the site), so the test is presence + count, not
identity. Those two checks move the score, so the journey reports a real
pass/fail instead of an all-INFO 0/0.
"""

import re
from typing import Dict, List, Set

from playwright.sync_api import Error as PlaywrightError, Page

from browser import audit_page
from core import CheckResult, Severity, accept_consent, failed_check

_RE_GTM_ID = re.compile(r'GTM-[A-Z0-9]{4,}')
_RE_GA4_ID = re.compile(r'G-[A-Z0-9]{6,}')

# vendor -> {"urls": [substrings in request URLs], "globals": [JS global names]}
_SIGNATURES: Dict[str, Dict[str, List[str]]] = {
    "Google Analytics 4":   {"urls": ["google-analytics.com/g/collect", "/g/collect"], "globals": ["gtag"]},
    "Google Tag Manager":   {"urls": ["googletagmanager.com/gtm.js"], "globals": ["google_tag_manager"]},
    "Google Ads / DoubleClick": {"urls": ["googleadservices.com", "googlesyndication.com", "doubleclick.net", "google.com/ads"], "globals": []},
    "Meta (Facebook) Pixel": {"urls": ["connect.facebook.net", "facebook.com/tr"], "globals": ["fbq"]},
    "TikTok Pixel":         {"urls": ["analytics.tiktok.com"], "globals": ["ttq"]},
    "LinkedIn Insight":     {"urls": ["snap.licdn.com", "px.ads.linkedin.com"], "globals": ["_linkedin_partner_id"]},
    "X (Twitter) Pixel":    {"urls": ["static.ads-twitter.com", "analytics.twitter.com", "t.co/i/adsct"], "globals": ["twq"]},
    "Pinterest Tag":        {"urls": ["ct.pinterest.com", "s.pinimg.com/ct"], "globals": ["pintrk"]},
    "Snapchat Pixel":       {"urls": ["tr.snapchat.com", "sc-static.net"], "globals": ["snaptr"]},
    "Microsoft / Bing UET": {"urls": ["bat.bing.com"], "globals": ["uetq"]},
    "Hotjar":               {"urls": ["static.hotjar.com", "script.hotjar.com"], "globals": ["hj"]},
    "Microsoft Clarity":    {"urls": ["clarity.ms"], "globals": ["clarity"]},
    "Segment":              {"urls": ["cdn.segment.com"], "globals": []},
    "HubSpot":              {"urls": ["js.hs-scripts.com", "hs-analytics.net"], "globals": ["_hsq"]},
    "Criteo":               {"urls": ["criteo.com", "criteo.net"], "globals": []},
    "Amplitude":            {"urls": ["amplitude.com", "api.amplitude.com"], "globals": ["amplitude"]},
    "Mixpanel":             {"urls": ["mixpanel.com", "api.mixpanel.com"], "globals": ["mixpanel"]},
    "Klaviyo":              {"urls": ["klaviyo.com", "static.klaviyo.com"], "globals": ["_learnq"]},
}


def _detect_globals(page: Page, names: List[str]) -> Set[str]:
    """Return the subset of JS global names that are defined on the page."""
    try:
        present = page.evaluate(
            "(names) => names.filter(function(n){return typeof window[n] !== 'undefined';})",
            names,
        )
        return set(present or [])
    except PlaywrightError:
        return set()


def _live_gtm_containers(page: Page) -> Set[str]:
    """GTM container IDs that are live on the page (most authoritative signal).
    Reads the keys of window.google_tag_manager, which GTM populates per
    container once gtm.js has run."""
    try:
        keys = page.evaluate(
            "Object.keys(window.google_tag_manager || {})"
        ) or []
    except PlaywrightError:
        return set()
    return {k for k in keys if k.startswith("GTM-")}


def _tag_presence_checks(
    page: Page, seen_urls: Set[str]
) -> List[CheckResult]:
    """Scored pass/fail checks for a public audit: is GTM running, and is GA4
    running? Presence + count, not identity — there's no expected ID to match
    on a site you have no access to. Each lists what was found."""
    url_blob = " ".join(seen_urls)
    found_gtm = sorted(_live_gtm_containers(page) | set(_RE_GTM_ID.findall(url_blob)))
    # GA4 IDs appear as id=G-XXXX (gtag/js loader) and tid=G-XXXX (g/collect hits).
    found_ga4 = sorted(set(_RE_GA4_ID.findall(url_blob)))

    return [
        CheckResult(
            name="GTM running",
            event=None,
            passed=bool(found_gtm),
            detail=(f"{len(found_gtm)} container(s): {', '.join(found_gtm)}"
                    if found_gtm else "No GTM container detected"),
            severity=Severity.HIGH,
        ),
        CheckResult(
            name="GA4 running",
            event=None,
            passed=bool(found_ga4),
            detail=(f"{len(found_ga4)} measurement ID(s): {', '.join(found_ga4)}"
                    if found_ga4 else
                    "No GA4 measurement ID detected (collect may require granted consent)"),
            severity=Severity.HIGH,
        ),
    ]


def run_tag_inventory_audit(url: str) -> List[CheckResult]:
    results: List[CheckResult] = []
    try:
        with audit_page(capture_network=True) as (page, recorder):
            return _inventory_checks(page, recorder, url)
    except PlaywrightError as exc:
        results.append(failed_check("tag_inventory", f"Browser error: {exc}", Severity.HIGH))
    return results


def _inventory_checks(page: Page, recorder, url: str) -> List[CheckResult]:
    results: List[CheckResult] = []
    page.goto(url)
    accept_consent(page)

    # Wait a short window for tags to fire asynchronously; the recorder
    # has been listening since before navigation.
    page.wait_for_timeout(8000)
    seen_urls: Set[str] = recorder.all_urls()

    all_globals = sorted({g for sig in _SIGNATURES.values() for g in sig["globals"]})
    present_globals = _detect_globals(page, all_globals)

    detected: List[str] = []
    for vendor, sig in _SIGNATURES.items():
        url_hits = [u for u in sig["urls"] if any(u in seen for seen in seen_urls)]
        global_hits = [g for g in sig["globals"] if g in present_globals]
        if not url_hits and not global_hits:
            continue
        detected.append(vendor)
        how = []
        if url_hits:
            how.append("network: " + ", ".join(sorted(set(url_hits))))
        if global_hits:
            how.append("JS global: " + ", ".join(global_hits))
        results.append(CheckResult(
            name=vendor, event=None, passed=True,
            detail=" | ".join(how), severity=Severity.INFO,
        ))

    # Scored checks: is GTM running, is GA4 running (presence + count).
    # HIGH severity so they count toward the journey score.
    scored = _tag_presence_checks(page, seen_urls)

    # Summary first-ish line for the report header.
    summary = CheckResult(
        name="Tags detected",
        event=None,
        passed=True,
        detail=(f"{len(detected)} vendor(s): {', '.join(detected)}" if detected
                else "No known analytics/advertising tags detected"),
        severity=Severity.INFO,
    )
    # Order in report: summary (INFO), scored gates, then per-vendor inventory.
    return [summary, *scored, *results]
