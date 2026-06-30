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
from network import extract_collect_requests, gcs_analytics_storage_granted
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


def _gtag_args(entry) -> Optional[list]:
    """
    Normalise a dataLayer entry into the positional arguments of a gtag() call,
    or None if it is not one.

    gtag() does `dataLayer.push(arguments)`, pushing the function's arguments
    object. Because that object is array-like but NOT a true Array, Selenium /
    JSON serialisation renders it either as:
      - a list  ['consent', 'default', {...}]                     (true Array), or
      - a dict  {'0': 'consent', '1': 'default', '2': {...}}      (arguments object)
    A normal dataLayer.push({...}) has named keys and yields None here.
    """
    if isinstance(entry, (list, tuple)):
        return list(entry)
    if isinstance(entry, dict) and "0" in entry:
        args, i = [], 0
        while str(i) in entry:
            args.append(entry[str(i)])
            i += 1
        return args
    return None


def _extract_consent_default(data_layer) -> Optional[dict]:
    """
    Return the first gtag consent-default state dict from a dataLayer list, or
    None if not found. Matches ('consent', 'default', {...}) in either the
    list-form or arguments-object (dict) form. Pure helper — unit-testable
    without a browser.
    """
    for entry in data_layer or []:
        args = _gtag_args(entry)
        if (
            args is not None
            and len(args) >= 3
            and args[0] == "consent"
            and args[1] == "default"
            and isinstance(args[2], dict)
        ):
            return args[2]
    return None


def _get_consent_mode_state(driver: webdriver.Chrome) -> Optional[dict]:
    """Read window.dataLayer and return the gtag consent-default state dict."""
    data_layer = driver.execute_script("return window.dataLayer || []") or []
    return _extract_consent_default(data_layer)


def _pre_consent_firing_result(pre_requests: List[dict]) -> CheckResult:
    """
    Classify pre-consent GA4 collect requests into a "Pre-consent GA4 firing"
    CheckResult.

    A collect request before consent is NOT automatically a violation. With
    Consent Mode in a default-denied state, GA4 sends cookieless pings (gcs
    shows analytics_storage denied) by design — that is compliant. The real
    violation is analytics_storage being *granted*, or no Consent Mode signal
    at all (gcs absent/unparseable), on a request that fired before the user
    made a choice.
    """
    if not pre_requests:
        return CheckResult(
            name="Pre-consent GA4 firing",
            event=None,
            passed=True,
            detail="No collect requests observed before consent interaction",
            severity=Severity.CRITICAL,
        )

    violations, compliant = [], []
    for r in pre_requests:
        en  = r["params"].get("en", "?")
        gcs = r["params"].get("gcs", "")
        if gcs_analytics_storage_granted(gcs) is False:
            compliant.append(f"{en} (gcs={gcs})")
        else:
            # analytics_storage granted, or no Consent Mode signal at all
            violations.append(f"{en} (gcs={gcs or 'absent'})")

    if violations:
        return failed_check(
            "Pre-consent GA4 firing",
            (
                f"{len(violations)} collect request(s) fired before consent with "
                f"analytics_storage granted or no Consent Mode signal: "
                f"[{', '.join(violations)}]"
            ),
            Severity.CRITICAL,
        )

    return CheckResult(
        name="Pre-consent GA4 firing",
        event=None,
        passed=True,
        detail=(
            f"{len(compliant)} cookieless Consent Mode ping(s) before consent "
            f"with analytics_storage denied — compliant: [{', '.join(compliant)}]"
        ),
        severity=Severity.CRITICAL,
    )


def _post_consent_transition_result(
    pre_requests: List[dict],
    post_requests: List[dict],
    consent_update_found: bool,
) -> CheckResult:
    """
    Verify analytics_storage actually transitions to *granted* after the accept
    click — the real proof that consent propagated.

    The authoritative signal is the gcs state of post-consent collect requests.
    Neither a consent_update dataLayer event nor the mere existence of
    post-consent collect requests proves analytics was granted: Consent Mode
    keeps sending cookieless (denied) pings regardless, so "any collect = pass"
    would mask a CMP that is not wired to Consent Mode.
    """
    post_granted = [
        r for r in post_requests
        if gcs_analytics_storage_granted(r["params"].get("gcs", "")) is True
    ]

    if post_granted:
        gcs_vals = sorted({r["params"].get("gcs", "") for r in post_granted})
        pre_granted = any(
            gcs_analytics_storage_granted(r["params"].get("gcs", "")) is True
            for r in pre_requests
        )
        transition = "already granted pre-consent" if pre_granted else "denied → granted"
        detail = (
            f"analytics_storage granted after accept ({transition}); "
            f"post-consent gcs={', '.join(gcs_vals)}"
        )
        if consent_update_found:
            detail += "; consent_update event also seen in dataLayer"
        return CheckResult(
            name="Post-consent update",
            event=None,
            passed=True,
            detail=detail,
            severity=Severity.HIGH,
        )

    if post_requests:
        gcs_seen = sorted({r["params"].get("gcs") or "absent" for r in post_requests})
        detail = (
            f"{len(post_requests)} collect request(s) fired after accepting consent but "
            f"analytics_storage never became granted (gcs={', '.join(gcs_seen)}) — "
            f"the CMP is not propagating the grant to Consent Mode"
        )
        if consent_update_found:
            detail += "; a consent_update event fired but gcs did not update"
        return failed_check("Post-consent update", detail, Severity.HIGH)

    if consent_update_found:
        return CheckResult(
            name="Post-consent update",
            event=None,
            passed=True,
            detail=(
                "consent_update event fired in dataLayer after accepting consent; "
                "no post-consent collect request seen to confirm the gcs grant"
            ),
            severity=Severity.HIGH,
        )

    return failed_check(
        "Post-consent update",
        (
            "No consent_update event and no collect requests after accepting consent — "
            "check config.CONSENT_ACCEPT_BUTTON selector"
        ),
        Severity.HIGH,
    )


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
        # Poll with a short timeout — on compliant sites the pre-consent pings
        # are denied, so we should not wait the full event timeout. The
        # compliant-vs-violation classification lives in a pure helper so it
        # can be unit-tested without a browser.
        pre_requests = extract_collect_requests(driver, timeout=5.0)
        results.append(_pre_consent_firing_result(pre_requests))

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

        # Authoritative check: did analytics_storage actually flip to granted
        # across the accept click? The consent_update dataLayer event is only a
        # supporting signal. Classification lives in a pure, unit-testable helper.
        results.append(_post_consent_transition_result(
            pre_requests, post_requests, consent_update_found
        ))

    except WebDriverException as exc:
        results.append(failed_check(
            "consent_audit", f"Driver error: {exc}", Severity.CRITICAL
        ))
    finally:
        driver.quit()

    return results
