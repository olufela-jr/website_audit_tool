"""
Tag & pixel inventory — publicly observable, no site access required.

Loads the page in Chrome, accepts consent, and records every network request
URL (via CDP performance logging) plus a set of known JavaScript globals. Each
is matched against a signature table of common analytics / advertising vendors
so the report can list "here is everything firing on your site".

Findings are INFO severity (an inventory, not pass/fail), so they don't move the
score.
"""

import json
import time
from typing import Dict, List, Set

from selenium import webdriver
from selenium.common.exceptions import WebDriverException

from core import CheckResult, Severity, accept_consent, failed_check, make_driver

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


def _drain_all_request_urls(driver: webdriver.Chrome) -> Set[str]:
    """Drain the CDP performance log and return every request URL seen.
    Destructive read — the buffer is cleared, so callers accumulate across calls."""
    urls: Set[str] = set()
    try:
        raw_logs = driver.get_log("performance")
    except Exception:  # noqa: BLE001
        return urls
    for entry in raw_logs:
        try:
            msg = json.loads(entry["message"])["message"]
            if msg.get("method") != "Network.requestWillBeSent":
                continue
            url = msg["params"]["request"].get("url", "")
            if url:
                urls.add(url)
        except (KeyError, json.JSONDecodeError, TypeError):
            continue
    return urls


def _detect_globals(driver: webdriver.Chrome, names: List[str]) -> Set[str]:
    """Return the subset of JS global names that are defined on the page."""
    try:
        present = driver.execute_script(
            "return arguments[0].filter(function(n){return typeof window[n] !== 'undefined';})",
            names,
        )
        return set(present or [])
    except WebDriverException:
        return set()


def run_tag_inventory_audit(url: str) -> List[CheckResult]:
    driver = make_driver(performance_logging=True)
    results: List[CheckResult] = []
    try:
        driver.get(url)
        accept_consent(driver)

        # Accumulate request URLs over a short window (tags fire asynchronously).
        seen_urls: Set[str] = set()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            seen_urls |= _drain_all_request_urls(driver)
            time.sleep(0.3)
        seen_urls |= _drain_all_request_urls(driver)

        all_globals = sorted({g for sig in _SIGNATURES.values() for g in sig["globals"]})
        present_globals = _detect_globals(driver, all_globals)

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

        # Summary first-ish line for the report header.
        summary = CheckResult(
            name="Tags detected",
            event=None,
            passed=True,
            detail=(f"{len(detected)} vendor(s): {', '.join(detected)}" if detected
                    else "No known analytics/advertising tags detected"),
            severity=Severity.INFO,
        )
        results.insert(0, summary)

    except WebDriverException as exc:
        results.append(failed_check("tag_inventory", f"Driver error: {exc}", Severity.HIGH))
    finally:
        driver.quit()

    return results
