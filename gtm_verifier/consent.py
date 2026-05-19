"""
Layer 2: Privacy & Consent Validation

Evaluates:
  - CMP banner / TCF API presence
  - Google Consent Mode implementation (default state + all 4 signals)
  - Pre-consent GA4 firing (Critical — fires before user gives consent?)
  - Post-consent update (does tracking change after accepting?)
"""

import time
from typing import List, Optional

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from core import (
    CheckResult,
    Severity,
    accept_consent,
    failed_check,
    get_datalayer_length,
    make_driver,
    skip_check,
)
from network import extract_collect_requests
import config


# ── CMP detection ─────────────────────────────────────────────────────────────

# Known vendor DOM signatures — ordered specific → generic
_CMP_SELECTORS = [
    ("#onetrust-consent-sdk",         "OneTrust"),
    ("#onetrust-banner-sdk",          "OneTrust"),
    ("#CybotCookiebotDialog",         "Cookiebot"),
    (".optanon-alert-box-wrapper",    "OneTrust (legacy)"),
    ("#truste-consent-content",       "TrustArc"),
    (".truste_overlay",               "TrustArc"),
    (".qc-cmp2-container",            "Quantcast"),
    ("#didomi-popup",                 "Didomi"),
    (".didomi-popup-container",       "Didomi"),
    ("#cookie-script-notice",         "Cookie Script"),
    (".clearcookie__inner",           "ClearCookie"),
    ("[id*='cookie-banner']",         "generic"),
    ("[class*='cookie-banner']",      "generic"),
    ("[id*='cookie-consent']",        "generic"),
    ("[class*='cookie-consent']",     "generic"),
    ("[id*='consent-banner']",        "generic"),
    ("[role='dialog'][aria-label*='cookie' i]",   "generic dialog"),
    ("[role='dialog'][aria-label*='consent' i]",  "generic dialog"),
]

_CONSENT_SIGNALS = [
    "analytics_storage",
    "ad_storage",
    "ad_user_data",
    "ad_personalization",
]


def _detect_cmp(driver: webdriver.Chrome) -> Optional[str]:
    """
    Scan the DOM for known CMP vendor signatures and the TCF __tcfapi.
    Returns a human-readable label of what was found, or None.
    """
    # TCF 2.0 API is the vendor-neutral signal — check first
    has_tcf = driver.execute_script("return typeof window.__tcfapi === 'function'")
    if has_tcf:
        return "TCF 2.0 API (__tcfapi) present"

    for selector, label in _CMP_SELECTORS:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, selector)
            if any(el.is_displayed() for el in els):
                return f"{label}  ({selector})"
        except Exception:
            continue
    return None


def _get_consent_mode_state(driver: webdriver.Chrome) -> Optional[dict]:
    """
    Return the first gtag consent default state dict from window.dataLayer,
    or None if not found.  Handles both list-form pushes
    (['consent', 'default', {...}]) and any dict-based variants.
    """
    data_layer = driver.execute_script("return window.dataLayer || []") or []
    for entry in data_layer:
        if (
            isinstance(entry, (list, tuple))
            and len(entry) >= 3
            and entry[0] == "consent"
            and entry[1] == "default"
            and isinstance(entry[2], dict)
        ):
            return entry[2]
    return None


# ── Public audit ──────────────────────────────────────────────────────────────

def run_consent_audit(url: str) -> List[CheckResult]:
    """
    Layer 2: Privacy & Consent audit.

    Loads the page WITHOUT accepting consent first so pre-consent collect
    requests can be detected, then accepts consent to verify the update.
    Uses a performance-logging driver for network capture.
    """
    driver = make_driver(performance_logging=True)
    results: List[CheckResult] = []
    try:
        driver.get(url)
        # Deliberately do NOT accept consent here yet — pre-consent checks first.

        # ── Check 1: CMP banner / TCF API present ─────────────────────────
        cmp_label = _detect_cmp(driver)
        if cmp_label:
            results.append(CheckResult(
                name="CMP banner detected",
                event=None,
                passed=True,
                detail=cmp_label,
                severity=Severity.MEDIUM,
            ))
        else:
            results.append(failed_check(
                "CMP banner detected",
                "No CMP banner or TCF API found in DOM — consent management may be absent",
                Severity.MEDIUM,
            ))

        # ── Check 2: Consent Mode implemented ────────────────────────────
        state = _get_consent_mode_state(driver)
        if state is not None:
            results.append(CheckResult(
                name="Consent Mode implemented",
                event=None,
                passed=True,
                detail=f"gtag consent default in dataLayer: {state}",
                severity=Severity.CRITICAL,
            ))
        else:
            results.append(failed_check(
                "Consent Mode implemented",
                "No gtag consent default push found in window.dataLayer — Consent Mode not configured",
                Severity.CRITICAL,
            ))

        # ── Check 3: Default state per signal ────────────────────────────
        if state is not None:
            for signal in _CONSENT_SIGNALS:
                value = state.get(signal)
                if value == "denied":
                    results.append(CheckResult(
                        name=f"Default denied: {signal}",
                        event=None,
                        passed=True,
                        detail=f"{signal} = 'denied'",
                        severity=Severity.HIGH,
                    ))
                elif value is None:
                    results.append(failed_check(
                        f"Default denied: {signal}",
                        f"{signal} not present in consent default — signal not configured",
                        Severity.HIGH,
                    ))
                else:
                    results.append(failed_check(
                        f"Default denied: {signal}",
                        f"{signal} = '{value}' (expected 'denied')",
                        Severity.HIGH,
                    ))
        else:
            for signal in _CONSENT_SIGNALS:
                results.append(skip_check(
                    f"Default denied: {signal}",
                    "Skipped — Consent Mode not implemented",
                    Severity.HIGH,
                ))

        # ── Check 4: Pre-consent GA4 firing ───────────────────────────────
        # Poll with a short timeout — on compliant sites nothing fires, so we
        # should not wait the full event timeout.
        pre_requests = extract_collect_requests(driver, timeout=5.0)
        if pre_requests:
            event_names = [r["params"].get("en", "?") for r in pre_requests]
            results.append(failed_check(
                "Pre-consent GA4 firing",
                (
                    f"{len(pre_requests)} collect request(s) fired before consent: "
                    f"events=[{', '.join(event_names)}]"
                ),
                Severity.CRITICAL,
            ))
        else:
            results.append(CheckResult(
                name="Pre-consent GA4 firing",
                event=None,
                passed=True,
                detail="No collect requests observed before consent interaction",
                severity=Severity.CRITICAL,
            ))

        # ── Check 5: Post-consent update ──────────────────────────────────
        pre_accept_idx = get_datalayer_length(driver)
        accept_consent(driver)

        # Consent update events vary by CMP — check common names and nested structures
        _CONSENT_UPDATE_EVENTS = {
            "consent_update",       # generic / custom GTM
            "gtm_consent_update",   # ClearCookie via GTM
            "clearcookie_save_preferences",  # ClearCookie native
            "CookieInformationConsentGiven", # Cookie Information
            "cookieyes-consent-update",      # CookieYes
        }

        consent_update_found = False
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            dl = driver.execute_script("return window.dataLayer || []") or []
            for entry in dl[pre_accept_idx:]:
                if not isinstance(entry, dict):
                    continue
                # Top-level event key
                if entry.get("event") in _CONSENT_UPDATE_EVENTS:
                    consent_update_found = True
                    break
                # Nested value.event (GTM internal consent push pattern)
                if isinstance(entry.get("value"), dict):
                    if entry["value"].get("event") in _CONSENT_UPDATE_EVENTS:
                        consent_update_found = True
                        break
            if consent_update_found:
                break
            time.sleep(0.3)

        post_requests = extract_collect_requests(driver, timeout=5.0)

        if consent_update_found:
            results.append(CheckResult(
                name="Post-consent update",
                event=None,
                passed=True,
                detail="consent_update event fired in dataLayer after accepting consent",
                severity=Severity.HIGH,
            ))
        elif post_requests:
            results.append(CheckResult(
                name="Post-consent update",
                event=None,
                passed=True,
                detail=(
                    f"No consent_update event, but {len(post_requests)} collect request(s) "
                    "fired post-consent — grant likely sent directly via gtag()"
                ),
                severity=Severity.HIGH,
            ))
        else:
            results.append(failed_check(
                "Post-consent update",
                (
                    "No consent_update in dataLayer and no collect requests after accepting consent — "
                    "check config.CONSENT_ACCEPT_BUTTON selector"
                ),
                Severity.HIGH,
            ))

    except WebDriverException as exc:
        results.append(failed_check(
            "consent_audit", f"Driver error: {exc}", Severity.CRITICAL
        ))
    finally:
        driver.quit()

    return results
