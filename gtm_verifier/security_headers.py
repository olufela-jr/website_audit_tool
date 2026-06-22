"""
Security headers audit — publicly observable, no site access required.

Inspects the HTTP response headers of the page for the common defensive headers
(HSTS, CSP, anti-clickjacking, MIME sniffing, referrer/permissions policy) and
checks that HTTP upgrades to HTTPS. Pure HTTP, no browser.
"""

from typing import List
from urllib.parse import urlparse

from core import CheckResult, Severity, failed_check
from httpfetch import fetch


def _ok(name, detail, sev) -> CheckResult:
    return CheckResult(name=name, event=None, passed=True, detail=detail, severity=sev)


def _bad(name, detail, sev) -> CheckResult:
    return CheckResult(name=name, event=None, passed=False, detail=detail, severity=sev)


def run_security_headers_audit(url: str) -> List[CheckResult]:
    resp = fetch(url)
    if not resp.ok:
        return [failed_check("Security fetch", f"Could not fetch site: {resp.error}", Severity.HIGH)]

    h = resp.headers
    results: List[CheckResult] = []
    final_https = urlparse(resp.final_url).scheme == "https"

    # ── HTTPS enforced ───────────────────────────────────────────────────────
    if url.startswith("http://") and final_https:
        results.append(_ok("HTTPS upgrade", f"HTTP redirected to {resp.final_url}", Severity.HIGH))
    elif final_https:
        results.append(_ok("HTTPS", "Served over HTTPS", Severity.HIGH))
    else:
        results.append(_bad("HTTPS", "Final response is not HTTPS", Severity.CRITICAL))

    # ── HSTS (only meaningful over HTTPS) ────────────────────────────────────
    hsts = h.get("strict-transport-security")
    if not final_https:
        pass  # covered by HTTPS check above
    elif hsts:
        results.append(_ok("HSTS", hsts, Severity.HIGH))
    else:
        results.append(_bad("HSTS", "No Strict-Transport-Security header", Severity.HIGH))

    # ── Content-Security-Policy ──────────────────────────────────────────────
    csp = h.get("content-security-policy")
    if csp:
        results.append(_ok("Content-Security-Policy", f"present ({len(csp)} chars)", Severity.HIGH))
    else:
        results.append(_bad(
            "Content-Security-Policy", "No CSP header (XSS / injection exposure)", Severity.HIGH,
        ))

    # ── Clickjacking protection ──────────────────────────────────────────────
    xfo = h.get("x-frame-options")
    frame_ancestors = "frame-ancestors" in (csp or "").lower()
    if xfo or frame_ancestors:
        results.append(_ok(
            "Clickjacking protection",
            xfo or "CSP frame-ancestors", Severity.MEDIUM,
        ))
    else:
        results.append(_bad(
            "Clickjacking protection",
            "No X-Frame-Options and no CSP frame-ancestors", Severity.MEDIUM,
        ))

    # ── MIME sniffing ────────────────────────────────────────────────────────
    xcto = h.get("x-content-type-options", "").lower()
    if "nosniff" in xcto:
        results.append(_ok("X-Content-Type-Options", "nosniff", Severity.MEDIUM))
    else:
        results.append(_bad("X-Content-Type-Options", "Missing 'nosniff'", Severity.MEDIUM))

    # ── Referrer-Policy ──────────────────────────────────────────────────────
    ref = h.get("referrer-policy")
    results.append(
        _ok("Referrer-Policy", ref, Severity.LOW) if ref
        else _bad("Referrer-Policy", "No Referrer-Policy header", Severity.LOW)
    )

    # ── Permissions-Policy ───────────────────────────────────────────────────
    pp = h.get("permissions-policy")
    results.append(
        _ok("Permissions-Policy", "present", Severity.LOW) if pp
        else _bad("Permissions-Policy", "No Permissions-Policy header", Severity.LOW)
    )

    # ── Version disclosure (informational) ───────────────────────────────────
    disclosed = {k: h[k] for k in ("server", "x-powered-by") if h.get(k)}
    results.append(CheckResult(
        name="Server disclosure",
        event=None,
        passed=True,
        detail=(", ".join(f"{k}: {v}" for k, v in disclosed.items()) if disclosed
                else "No Server / X-Powered-By header"),
        severity=Severity.INFO,
    ))

    return results
