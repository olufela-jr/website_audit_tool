"""
Security headers audit — PUBLIC stream (outside-in, no access required).

Inspects the HTTP response headers of the page for the common defensive headers
(HSTS, CSP, anti-clickjacking, MIME sniffing, referrer/permissions policy) and
checks that HTTP upgrades to HTTPS. Response headers are publicly observable, so
this is a public audit.

Fetching goes through `resilient_fetch`: a fast raw HTTP request, falling back to
an in-browser same-origin fetch (which can read response headers) when a WAF/bot
filter refuses the raw client. It is SKIPPED (score-neutral) only if neither path
can reach the site — a defended perimeter must never be scored as insecure.
"""

from typing import List
from urllib.parse import urlparse

from browser_fetch import resilient_fetch as fetch
from core import CheckResult, Severity, skip_check

# Used only when both the raw and in-browser fetch fail — a transport dead-end,
# not a site defect, so it is skipped (never scored as a failure).
_UNREACHABLE = (
    "Not assessed — the site could not be reached (raw and in-browser "
    "fetch both failed)."
)


def _ok(name, detail, sev) -> CheckResult:
    return CheckResult(name=name, event=None, passed=True, detail=detail, severity=sev)


def _bad(name, detail, sev) -> CheckResult:
    return CheckResult(name=name, event=None, passed=False, detail=detail, severity=sev)


def run_security_headers_audit(url: str) -> List[CheckResult]:
    resp = fetch(url)
    if not resp.ok:
        return [skip_check("Security headers", _UNREACHABLE, Severity.HIGH)]

    h = resp.headers
    # Guard against a false-negative sweep: if the site was reached but no headers
    # were captured, every header would "fail". That's a measurement gap, not an
    # insecure site — skip and say so rather than report seven false failures.
    if not h:
        return [skip_check(
            "Security headers",
            f"Reached the site ({resp.source}, HTTP {resp.status}) but no response "
            f"headers were captured — cannot assess. Re-run to retry.",
            Severity.HIGH,
        )]

    src = resp.source
    nh = len(h)
    # Provenance row so every failure below can be read against what was actually
    # observed (how it was fetched, the status, and how many headers were seen).
    where = f"via {src}; checked {nh} response headers"
    results: List[CheckResult] = [CheckResult(
        name="Fetch",
        event=None,
        passed=True,
        detail=f"{src} · HTTP {resp.status} · {nh} response headers · final URL {resp.final_url}",
        severity=Severity.INFO,
    )]
    final_https = urlparse(resp.final_url).scheme == "https"

    # ── HTTPS enforced ───────────────────────────────────────────────────────
    if url.startswith("http://") and final_https:
        results.append(_ok("HTTPS upgrade", f"HTTP redirected to {resp.final_url}", Severity.HIGH))
    elif final_https:
        results.append(_ok("HTTPS", f"Served over HTTPS (final URL: {resp.final_url})", Severity.HIGH))
    else:
        results.append(_bad(
            "HTTPS",
            f"Final response is not HTTPS — served over plain HTTP (final URL: {resp.final_url}).",
            Severity.CRITICAL,
        ))

    # ── HSTS (only meaningful over HTTPS) ────────────────────────────────────
    hsts = h.get("strict-transport-security")
    if not final_https:
        pass  # covered by HTTPS check above
    elif hsts:
        results.append(_ok("HSTS", hsts, Severity.HIGH))
    else:
        results.append(_bad(
            "HSTS",
            f"No 'strict-transport-security' header in the response ({where}). "
            "Site is HTTPS, so HSTS should be set to block protocol-downgrade attacks.",
            Severity.HIGH,
        ))

    # ── Content-Security-Policy ──────────────────────────────────────────────
    csp = h.get("content-security-policy")
    if csp:
        results.append(_ok("Content-Security-Policy", f"present ({len(csp)} chars)", Severity.HIGH))
    else:
        results.append(_bad(
            "Content-Security-Policy",
            f"No 'content-security-policy' header in the response ({where}) — "
            "nothing constraining script/resource origins (XSS / injection exposure).",
            Severity.HIGH,
        ))

    # ── Clickjacking protection ──────────────────────────────────────────────
    xfo = h.get("x-frame-options")
    frame_ancestors = "frame-ancestors" in (csp or "").lower()
    if xfo or frame_ancestors:
        results.append(_ok(
            "Clickjacking protection",
            f"x-frame-options: {xfo}" if xfo else "CSP frame-ancestors directive present",
            Severity.MEDIUM,
        ))
    else:
        results.append(_bad(
            "Clickjacking protection",
            f"Neither 'x-frame-options' nor a CSP 'frame-ancestors' directive present "
            f"({where}; CSP {'present but without frame-ancestors' if csp else 'absent'}) — "
            "the page can be framed for clickjacking.",
            Severity.MEDIUM,
        ))

    # ── MIME sniffing ────────────────────────────────────────────────────────
    xcto = h.get("x-content-type-options", "").lower()
    if "nosniff" in xcto:
        results.append(_ok("X-Content-Type-Options", "nosniff", Severity.MEDIUM))
    else:
        results.append(_bad(
            "X-Content-Type-Options",
            f"'x-content-type-options' is {repr(xcto) if xcto else 'absent'}, expected 'nosniff' "
            f"({where}) — browsers may MIME-sniff responses.",
            Severity.MEDIUM,
        ))

    # ── Referrer-Policy ──────────────────────────────────────────────────────
    ref = h.get("referrer-policy")
    results.append(
        _ok("Referrer-Policy", ref, Severity.LOW) if ref
        else _bad("Referrer-Policy", f"No 'referrer-policy' header in the response ({where}).", Severity.LOW)
    )

    # ── Permissions-Policy ───────────────────────────────────────────────────
    pp = h.get("permissions-policy")
    results.append(
        _ok("Permissions-Policy", "present", Severity.LOW) if pp
        else _bad("Permissions-Policy", f"No 'permissions-policy' header in the response ({where}).", Severity.LOW)
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
