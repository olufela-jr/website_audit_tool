"""
Layer 3: Event Detection & Session Reconstruction

Captures GA4 collect (v=2) requests via Chrome CDP performance logging.
No proxy or network interception — purely in-browser via Selenium performance logs.
"""

import json
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlparse

from selenium import webdriver
from selenium.common.exceptions import WebDriverException

from core import (
    CheckResult,
    Severity,
    accept_consent,
    failed_check,
    make_driver,
    skip_check,
)
import config


# ── Internal helpers ──────────────────────────────────────────────────────────

def _drain_performance_log(driver: webdriver.Chrome) -> List[dict]:
    """
    Drain the CDP performance log buffer (destructive — entries are cleared on read).
    Returns parsed GA4 collect request dicts with keys: url, params, timestamp.
    Only entries matching /collect with v=2 are included.
    """
    entries = []
    try:
        raw_logs = driver.get_log("performance")
    except Exception:
        return entries

    for entry in raw_logs:
        try:
            # Selenium wraps CDP events as {"message": {"method": ..., "params": ...}, "webview": ...}
            outer = json.loads(entry["message"])
            msg = outer["message"]
            if msg.get("method") != "Network.requestWillBeSent":
                continue
            request = msg["params"]["request"]
            url = request.get("url", "")
            if "/collect" not in url:
                continue
            parsed = urlparse(url)
            params = dict(parse_qsl(parsed.query))
            if params.get("v") != "2":
                continue
            # Merge POST body params when present (GA4 sends extra data there)
            post_data = request.get("postData", "")
            if post_data:
                params.update(dict(parse_qsl(post_data)))
            entries.append({
                "url":       url,
                "params":    params,
                "timestamp": msg["params"].get("timestamp", 0.0),
            })
        except (KeyError, json.JSONDecodeError, TypeError):
            continue
    return entries


def _poll_collect_requests(
    driver: webdriver.Chrome,
    timeout: float = 8.0,
) -> List[dict]:
    """
    Poll the CDP performance log every 300 ms until at least one GA4 collect
    request appears or timeout expires.  Accumulates across multiple drains
    since the log buffer is cleared on each read.
    """
    collected: List[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        batch = _drain_performance_log(driver)
        collected.extend(batch)
        if collected:
            return collected
        time.sleep(0.3)
    return collected


# ── Public API ────────────────────────────────────────────────────────────────

def extract_collect_requests(
    driver: webdriver.Chrome,
    timeout: float = 8.0,
) -> List[dict]:
    """
    Return all GA4 collect requests seen since the last drain.
    Called by consent.py to check pre/post-consent request state.
    Returns empty list (silently) if the driver was not created with
    performance_logging=True.
    """
    return _poll_collect_requests(driver, timeout=timeout)


def run_network_audit(url: str) -> List[CheckResult]:
    """
    Layer 3 network audit. Spins up its own driver with performance logging,
    loads url, accepts consent, then analyses all GA4 collect requests.
    """
    driver = make_driver(performance_logging=True)
    results: List[CheckResult] = []
    try:
        driver.get(url)
        accept_consent(driver)

        requests = extract_collect_requests(driver, timeout=10.0)

        # ── Check 1: GA4 collect requests present ─────────────────────────
        if not requests:
            results.append(CheckResult(
                name="GA4 collect requests",
                event=None,
                passed=False,
                detail="No GA4 collect (v=2) requests observed in performance log",
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

    except WebDriverException as exc:
        results.append(failed_check(
            "network_audit", f"Driver error: {exc}", Severity.CRITICAL
        ))
    finally:
        driver.quit()

    return results


# ── GCS decoder ──────────────────────────────────────────────────────────────

def _interpret_gcs(gcs: str) -> str:
    """
    Best-effort human-readable decode of the GA4 gcs consent state parameter.
    Format: G[ad_storage][analytics_storage][ad_user_data][ad_personalization]
    where 1 = granted, 0 = denied.
    """
    _labels = ["ad_storage", "analytics_storage", "ad_user_data", "ad_personalization"]
    digits = gcs.lstrip("G")
    if not digits or not all(c in "01" for c in digits):
        return gcs  # unrecognised format — return raw
    parts = []
    for i, label in enumerate(_labels):
        if i < len(digits):
            parts.append(f"{label}={'granted' if digits[i] == '1' else 'denied'}")
    return ", ".join(parts) if parts else gcs
