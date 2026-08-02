"""
Layer 2: Privacy & Consent Validation

Evaluates:
  - CMP banner / TCF API presence
  - Google Consent Mode implementation (default state + all 4 signals)
  - Pre-consent GA4 firing (Critical — fires before user gives consent?)
  - Post-consent update (does tracking change after accepting?)
"""

import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, Page

from browser import audit_page
from core import (
    CheckResult,
    Severity,
    _candidate_frames,
    accept_consent,
    failed_check,
    get_datalayer_length,
    skip_check,
)
from network import gcs_analytics_storage_granted
import config

# Live window.dataLayer read with a per-entry sanitizer: Playwright's evaluate
# serialisation throws on circular refs (e.g. gtm.element DOM nodes) where
# Selenium degraded gracefully, so each entry is JSON-roundtripped in-page and
# unserialisable ones become null (skipped by the pure helpers).
_DL_LIVE_READ_JS = """
(window.dataLayer || []).map(e => {
    try { return JSON.parse(JSON.stringify(e)); } catch (err) { return null; }
})
"""


# ── CMP detection ─────────────────────────────────────────────────────────────
#
# Tiered, vendor-neutral-first: every poll iteration tries all three tiers and
# the first (or strongest) evidence found wins. Confidence weights and the
# poll timeout live in config.py as tunables — placeholders, not yet
# validated against a real sample of sites.
#
#   Tier 1 (primary):     IAB TCF/GPP standards APIs + gtag consent commands
#                         in dataLayer. No vendor-specific knowledge required —
#                         every certified CMP must expose __tcfapi or __gpp,
#                         and any Consent Mode integration pushes to dataLayer.
#   Tier 2:               Known CMP CDN request hostnames. Low-maintenance (a
#                         URL substring, not DOM structure) but still
#                         vendor-specific.
#   Tier 3 (tail-catcher): Curated DOM selectors, scanned across the top
#                         document and candidate iframes. Playwright's locator
#                         CSS engine pierces open shadow roots automatically,
#                         so shadow-DOM CMPs are covered with no extra code.
#
# A single synchronous snapshot right after page.goto() races CMP scripts that
# inject their banner asynchronously — detect_cmp() polls all three tiers
# together up to config.CMP_DETECTION_TIMEOUT instead of trusting one snapshot.

@dataclass
class CmpDetection:
    """Result of tiered CMP detection.

    `present` and `banner_visible` are deliberately distinct: a CMP can be
    present (Consent Mode wired up, TCF API exposed) without a banner being
    on-screen right now (e.g. consent already stored from a prior visit), and
    a visible dialog is the strongest but not the only proof of presence.
    """
    present: bool
    banner_visible: bool
    confidence: float
    vendor: Optional[str]
    evidence: List[str]


# Snapshot of IAB TCF CMP IDs -> vendor name, per Google's certified-CMP list
# (https://support.google.com/adsense/answer/13554116). NOT exhaustive — the
# full IAB registry has hundreds of entries and changes over time. An unmapped
# id still counts as "CMP present", just reported without a vendor name.
_TCF_CMP_ID_TO_NAME = {
    5:   "Usercentrics",
    7:   "Didomi",
    28:  "OneTrust",
    123: "Iubenda",
    134: "Cookiebot",
}

# Known CMP CDN request hostnames — low-maintenance vendor signal (a URL
# substring match against every request the page makes, not DOM structure).
# Non-exhaustive; extend as new vendors are seen in the field.
_CMP_CDN_HOSTS = {
    "cdn.cookielaw.org":        "OneTrust",
    "geolocation.onetrust.com": "OneTrust",
    "consent.cookiebot.com":    "Cookiebot",
    "consentcdn.cookiebot.com": "Cookiebot",
    "cs.iubenda.com":           "Iubenda",
    "cdn.iubenda.com":          "Iubenda",
    "sdk.privacy-center.org":   "Didomi",
    "api.didomi.io":            "Didomi",
    "app.usercentrics.eu":      "Usercentrics",
    "cmp.osano.com":            "Osano",
    "cmp.quantcast.com":        "Quantcast",
}

# Selectors kept only as an interim stopgap for vendors not yet reliably
# caught by Tier 1 or Tier 2 alone — remove once a live regression check
# confirms those tiers catch them independently (tails.com, the site that
# prompted this, is already caught by Tier 2 via cs.iubenda.com/cdn.iubenda.com).
_STOPGAP_SELECTOR_LABELS = {"Iubenda"}

# Known vendor DOM signatures — ordered specific → generic (Tier 3)
_CMP_SELECTORS = [
    ("#onetrust-consent-sdk",         "OneTrust"),
    ("#onetrust-banner-sdk",          "OneTrust"),
    ("#CybotCookiebotDialog",         "Cookiebot"),
    (".optanon-alert-box-wrapper",    "OneTrust (legacy)"),
    ("#truste-consent-content",       "TrustArc"),
    (".truste_overlay",               "TrustArc"),
    (".qc-cmp2-container",            "Quantcast"),
    ("#iubenda-cs-banner",            "Iubenda"),           # stopgap
    (".iubenda-cs-container",         "Iubenda"),           # stopgap
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

# One-shot callback-style TCF/GPP "ping" commands, wrapped in a Promise so
# Playwright's evaluate() can await them. Both resolve to None if the API
# isn't present, the call throws, or it doesn't answer within 1s.
_TCF_PING_JS = """
() => new Promise(resolve => {
    if (typeof window.__tcfapi !== 'function') { resolve(null); return; }
    let done = false;
    const timer = setTimeout(() => { if (!done) { done = true; resolve(null); } }, 1000);
    try {
        window.__tcfapi('ping', 2, (pingReturn, success) => {
            if (done) return;
            done = true; clearTimeout(timer);
            resolve(success ? pingReturn : null);
        });
    } catch (e) {
        if (!done) { done = true; clearTimeout(timer); resolve(null); }
    }
})
"""

_GPP_PING_JS = """
() => new Promise(resolve => {
    if (typeof window.__gpp !== 'function') { resolve(null); return; }
    let done = false;
    const timer = setTimeout(() => { if (!done) { done = true; resolve(null); } }, 1000);
    try {
        window.__gpp('ping', (pingData, success) => {
            if (done) return;
            done = true; clearTimeout(timer);
            resolve(success ? pingData : null);
        });
    } catch (e) {
        if (!done) { done = true; clearTimeout(timer); resolve(null); }
    }
})
"""


def _has_gtag_consent_command(data_layer) -> bool:
    """True if a gtag('consent', 'default'|'update', {...}) push is present.

    Vendor-neutral Tier 1 evidence: any Consent Mode integration must push one
    of these regardless of which CMP wires it up. Pure helper — unit-testable
    without a browser.
    """
    for entry in data_layer or []:
        args = _gtag_args(entry)
        if (
            args is not None
            and len(args) >= 2
            and args[0] == "consent"
            and args[1] in ("default", "update")
        ):
            return True
    return False


def _match_cdn_host(hosts: Iterable[str]) -> Optional[Tuple[str, str]]:
    """First (host, vendor) from _CMP_CDN_HOSTS matching a seen hostname
    (exact or subdomain). `hosts` are already-lowercased netlocs. Pure
    helper — unit-testable without a browser."""
    hosts = set(hosts)
    for host, vendor in _CMP_CDN_HOSTS.items():
        if any(h == host or h.endswith("." + host) for h in hosts):
            return host, vendor
    return None


def _tier1_standards_signals(page: Page) -> Tuple[Optional[str], float, List[str]]:
    """TCF/GPP ping + gtag consent commands — vendor-neutral primary signal."""
    vendor: Optional[str] = None
    confidence = 0.0
    evidence: List[str] = []

    tcf = page.evaluate(_TCF_PING_JS)
    if tcf is not None:
        cmp_id = tcf.get("cmpId") if isinstance(tcf, dict) else None
        vendor = _TCF_CMP_ID_TO_NAME.get(cmp_id)
        evidence.append(
            f"TCF 2.0 API (__tcfapi) present, cmpId={cmp_id}"
            + (f" -> {vendor}" if vendor else " (vendor not in local snapshot)")
        )
        confidence = max(confidence, config.CMP_TIER1_CONFIDENCE)

    gpp = page.evaluate(_GPP_PING_JS)
    if gpp is not None:
        evidence.append("GPP API (__gpp) present")
        confidence = max(confidence, config.CMP_TIER1_CONFIDENCE)

    data_layer = page.evaluate(_DL_LIVE_READ_JS) or []
    if _has_gtag_consent_command(data_layer):
        evidence.append("gtag consent default/update command in dataLayer")
        confidence = max(confidence, config.CMP_TIER1_CONFIDENCE)

    return vendor, confidence, evidence


def _tier2_cdn_hosts(recorder) -> Tuple[Optional[str], float, List[str]]:
    """Known CMP CDN request hostname, from requests already captured by the
    shared NetworkRecorder. No changes needed to that class — all_urls()
    already returns full URLs, which is all a hostname match requires."""
    if recorder is None:
        return None, 0.0, []
    hosts = {urlparse(u).netloc.lower() for u in recorder.all_urls()}
    match = _match_cdn_host(hosts)
    if match is None:
        return None, 0.0, []
    host, vendor = match
    return vendor, config.CMP_TIER2_CONFIDENCE, [f"CMP CDN request host matched: {host} ({vendor})"]


def _scan_frame_for_cmp(frame) -> Optional[Tuple[str, str]]:
    """First visible (selector, label) match in one frame. Uses Playwright's
    locator CSS engine, which pierces open shadow roots automatically — no
    manual shadow-DOM walker needed."""
    for selector, label in _CMP_SELECTORS:
        try:
            loc = frame.locator(selector)
            # is_visible() takes an immediate snapshot (no auto-wait), matching
            # the old find_elements + is_displayed semantics.
            if any(loc.nth(i).is_visible() for i in range(loc.count())):
                return selector, label
        except Exception:
            continue
    return None


def _tier3_selectors_and_heuristic(page: Page) -> Tuple[Optional[str], float, List[str], bool]:
    """Curated selectors — the tail-catcher. Scans the top document first,
    then candidate iframes (CMP-looking ones first), reusing the same frame
    heuristic accept_consent() sweeps in core.py."""
    match = _scan_frame_for_cmp(page.main_frame)
    where = "top document"
    if match is None:
        for frame in _candidate_frames(page):
            match = _scan_frame_for_cmp(frame)
            if match is not None:
                where = f"iframe ({frame.url})"
                break
    if match is None:
        return None, 0.0, [], False

    selector, label = match
    note = " [stopgap selector]" if label in _STOPGAP_SELECTOR_LABELS else ""
    evidence = [f"DOM selector matched: {selector} ({label}) in {where}{note}"]
    return label, config.CMP_TIER3_CONFIDENCE, evidence, True


def detect_cmp(page: Page, recorder=None) -> CmpDetection:
    """
    Tiered CMP detection (see the section comment above for the tier
    rationale). Polls all three tiers together up to
    config.CMP_DETECTION_TIMEOUT so a banner that injects asynchronously
    after load doesn't race a single synchronous snapshot.
    """
    deadline = time.monotonic() + config.CMP_DETECTION_TIMEOUT
    while True:
        vendor1, conf1, ev1 = _tier1_standards_signals(page)
        vendor2, conf2, ev2 = _tier2_cdn_hosts(recorder)
        vendor3, conf3, ev3, banner_visible = _tier3_selectors_and_heuristic(page)

        evidence = ev1 + ev2 + ev3
        if evidence or time.monotonic() >= deadline:
            return CmpDetection(
                present=bool(evidence),
                banner_visible=banner_visible,
                confidence=max(conf1, conf2, conf3),
                vendor=vendor1 or vendor2 or vendor3,
                evidence=evidence,
            )
        page.wait_for_timeout(config.CMP_DETECTION_POLL_MS)


def _gtag_args(entry) -> Optional[list]:
    """
    Normalise a dataLayer entry into the positional arguments of a gtag() call,
    or None if it is not one.

    gtag() does `dataLayer.push(arguments)`, pushing the function's arguments
    object. Because that object is array-like but NOT a true Array, JSON
    serialisation renders it either as:
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


def _get_consent_mode_state(page: Page) -> Optional[dict]:
    """Read window.dataLayer and return the gtag consent-default state dict."""
    data_layer = page.evaluate(_DL_LIVE_READ_JS) or []
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
    The fresh browser context guarantees no stored consent state carries over.
    """
    results: List[CheckResult] = []
    try:
        with audit_page(capture_network=True) as (page, recorder):
            return _consent_checks(page, recorder, url)
    except PlaywrightError as exc:
        results.append(failed_check(
            "consent_audit", f"Browser error: {exc}", Severity.CRITICAL
        ))
    return results


def _consent_checks(page: Page, recorder, url: str) -> List[CheckResult]:
    results: List[CheckResult] = []
    page.goto(url)
    # Deliberately do NOT accept consent here yet — pre-consent checks first.

    # ── Check 1: CMP banner / TCF API present ─────────────────────────
    detection = detect_cmp(page, recorder)
    if detection.present:
        vendor_str = f", vendor={detection.vendor}" if detection.vendor else ""
        results.append(CheckResult(
            name="CMP banner detected",
            event=None,
            passed=True,
            detail=(
                f"confidence={detection.confidence:.2f}, "
                f"banner_visible={detection.banner_visible}{vendor_str}; "
                f"evidence: {'; '.join(detection.evidence)}"
            ),
            severity=Severity.MEDIUM,
        ))
    else:
        results.append(failed_check(
            "CMP banner detected",
            (
                f"No CMP standards API, known CMP CDN request, or banner selector "
                f"observed within {config.CMP_DETECTION_TIMEOUT:.0f}s from this vantage "
                f"point — not conclusive proof of absence: detection can vary by "
                f"geography, bot-protection challenges, or consent-string region"
            ),
            Severity.MEDIUM,
        ))

    # ── Check 2: Consent Mode implemented ────────────────────────────
    state = _get_consent_mode_state(page)
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
    pre_requests = recorder.drain_collect(timeout=5.0)
    results.append(_pre_consent_firing_result(pre_requests))

    # ── Check 5: Post-consent update ──────────────────────────────────
    pre_accept_idx = get_datalayer_length(page)
    accept_consent(page)

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
        dl = page.evaluate(_DL_LIVE_READ_JS) or []
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
        page.wait_for_timeout(300)

    post_requests = recorder.drain_collect(timeout=5.0)

    # Authoritative check: did analytics_storage actually flip to granted
    # across the accept click? The consent_update dataLayer event is only a
    # supporting signal. Classification lives in a pure, unit-testable helper.
    results.append(_post_consent_transition_result(
        pre_requests, post_requests, consent_update_found
    ))

    return results
