"""
Layer 3: Event Detection & Session Reconstruction

Captures GA4 collect (v=2) requests via Playwright request events
(browser.NetworkRecorder). No proxy — purely in-browser capture.
"""

from typing import Dict, List, Optional, Tuple

from playwright.sync_api import Error as PlaywrightError

from browser import audit_page
from core import (
    CheckResult,
    Severity,
    accept_consent,
    failed_check,
    skip_check,
)


def run_network_audit(url: str) -> List[CheckResult]:
    """
    Layer 3 network audit. Opens its own page with network capture, loads url,
    accepts consent, then analyses all GA4 collect requests.
    """
    results: List[CheckResult] = []
    try:
        with audit_page(capture_network=True) as (page, recorder):
            return _network_checks(page, recorder, url)
    except PlaywrightError as exc:
        results.append(failed_check(
            "network_audit", f"Browser error: {exc}", Severity.CRITICAL
        ))
    return results


def _network_checks(page, recorder, url: str) -> List[CheckResult]:
    results: List[CheckResult] = []
    page.goto(url)
    accept_consent(page)

    requests = recorder.drain_collect(timeout=10.0)

    # ── Check 1: GA4 collect requests present ─────────────────────────
    if not requests:
        results.append(CheckResult(
            name="GA4 collect requests",
            event=None,
            passed=False,
            detail="No GA4 collect (v=2) requests observed on the network",
            severity=Severity.CRITICAL,
        ))
        for name, sev in [
            ("Client ID (cid)",          Severity.HIGH),
            ("Session ID (sid)",          Severity.HIGH),
            ("Consent state (gcs)",       Severity.HIGH),
        ]:
            results.append(skip_check(name, "No collect requests to inspect", sev))
        results.append(skip_check("Event inventory",  "No collect requests found", Severity.INFO))
        results.append(skip_check("Session timeline", "No collect requests found", Severity.INFO))
        return results

    results.append(CheckResult(
        name="GA4 collect requests",
        event=None,
        passed=True,
        detail=f"{len(requests)} collect request(s) observed",
        severity=Severity.CRITICAL,
    ))

    # ── Check 2: Client ID ────────────────────────────────────────────
    cid_values = sorted({r["params"]["cid"] for r in requests if "cid" in r["params"]})
    if cid_values:
        results.append(CheckResult(
            name="Client ID (cid)",
            event=None,
            passed=True,
            detail=f"cid: {', '.join(cid_values)}",
            severity=Severity.HIGH,
        ))
    else:
        results.append(failed_check(
            "Client ID (cid)",
            "cid param absent from all collect requests",
            Severity.HIGH,
        ))

    # ── Check 3: Session ID ───────────────────────────────────────────
    sid_values = sorted({r["params"]["sid"] for r in requests if "sid" in r["params"]})
    if sid_values:
        results.append(CheckResult(
            name="Session ID (sid)",
            event=None,
            passed=True,
            detail=f"sid: {', '.join(sid_values)}",
            severity=Severity.HIGH,
        ))
    else:
        results.append(failed_check(
            "Session ID (sid)",
            "sid param absent from all collect requests",
            Severity.HIGH,
        ))

    # ── Check 4: Consent state (gcs) ─────────────────────────────────
    gcs_values = sorted({r["params"]["gcs"] for r in requests if "gcs" in r["params"]})
    if gcs_values:
        interpretations = [_interpret_gcs(v) for v in gcs_values]
        results.append(CheckResult(
            name="Consent state (gcs)",
            event=None,
            passed=True,
            detail=f"gcs={', '.join(gcs_values)}  →  {'; '.join(interpretations)}",
            severity=Severity.HIGH,
        ))
    else:
        results.append(failed_check(
            "Consent state (gcs)",
            "gcs param absent — Consent Mode signals not present in collect requests",
            Severity.HIGH,
        ))

    # ── Check 5: Event inventory (INFO) ───────────────────────────────
    event_names = sorted({r["params"]["en"] for r in requests if "en" in r["params"]})
    results.append(CheckResult(
        name="Event inventory",
        event=None,
        passed=True,
        detail=(
            f"{len(event_names)} distinct event(s): {', '.join(event_names)}"
            if event_names else "(no en params decoded)"
        ),
        severity=Severity.INFO,
    ))

    # ── Check 6: Session timeline (INFO) ──────────────────────────────
    timeline: Dict[str, List[Tuple[float, str]]] = {}
    for req in requests:
        p   = req["params"]
        key = f"cid={p.get('cid', '?')} / sid={p.get('sid', '?')}"
        timeline.setdefault(key, []).append((req["timestamp"], p.get("en", "?")))
    for key in timeline:
        timeline[key].sort(key=lambda x: x[0])

    session_lines = []
    for key, events in timeline.items():
        event_seq = " → ".join(en for _, en in events)
        session_lines.append(f"[{key}]  {event_seq}")
    results.append(CheckResult(
        name="Session timeline",
        event=None,
        passed=True,
        detail=("\n        ".join(session_lines) if session_lines else "(no cid/sid data)"),
        severity=Severity.INFO,
    ))

    return results


# ── GCS decoder ──────────────────────────────────────────────────────────────

def _parse_gcs_digits(gcs: str) -> Optional[str]:
    """
    Return the digit string of a GA4 `gcs` parameter, or None if it is missing
    or not a recognised G-prefixed binary string.

    Format: G + [version flag] + [ad_storage] + [analytics_storage]
    (1 = granted, 0 = denied). Only these two signals are encoded in gcs;
    ad_user_data / ad_personalization live in the separate `gcd` parameter.
    """
    if not gcs:
        return None
    digits = gcs.lstrip("G")
    if len(digits) < 3 or not all(c in "01" for c in digits):
        return None
    return digits


def gcs_analytics_storage_granted(gcs: str) -> Optional[bool]:
    """
    Return the analytics_storage consent state encoded in a GA4 `gcs` parameter,
    or None if gcs is missing/unparseable. The third digit (index 2) is
    analytics_storage: G100 -> denied, G111/G101 -> granted.
    """
    digits = _parse_gcs_digits(gcs)
    return None if digits is None else digits[2] == "1"


def _interpret_gcs(gcs: str) -> str:
    """
    Best-effort human-readable decode of the GA4 gcs consent state parameter.
    Format: G + [version flag] + [ad_storage] + [analytics_storage] where
    1 = granted, 0 = denied. e.g. G100 = both denied, G111 = both granted,
    G101 = analytics_storage granted only.

    Note: ad_user_data / ad_personalization are NOT carried in gcs (they are
    encoded in the separate `gcd` parameter), so they are not reported here.
    """
    digits = _parse_gcs_digits(gcs)
    if digits is None:
        return gcs  # unrecognised format — return raw
    return (
        f"ad_storage={'granted' if digits[1] == '1' else 'denied'}, "
        f"analytics_storage={'granted' if digits[2] == '1' else 'denied'}"
    )
