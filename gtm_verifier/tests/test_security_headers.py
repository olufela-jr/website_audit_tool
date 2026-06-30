"""Unit tests for the security-headers audit. security_headers.fetch is
monkeypatched with crafted header dicts so no network is touched."""

import security_headers as sh
from core import Severity
from httpfetch import HttpResult


def _by_name(results):
    return {r.name: r for r in results}


def _resp(headers, final_url="https://example.com"):
    # Real responses always carry baseline headers (content-type, date) — include
    # them so "missing security headers" doesn't look like a capture failure.
    base = {"content-type": "text/html; charset=utf-8", "date": "Wed, 01 Jan 2025 00:00:00 GMT"}
    return lambda url, *a, **k: HttpResult(final_url, 200, {**base, **headers}, "")


def test_security_all_present(monkeypatch):
    headers = {
        "strict-transport-security": "max-age=63072000; includeSubDomains",
        "content-security-policy": "default-src 'self'",
        "x-frame-options": "DENY",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "permissions-policy": "geolocation=()",
        "server": "nginx",
    }
    monkeypatch.setattr(sh, "fetch", _resp(headers))
    res = _by_name(sh.run_security_headers_audit("https://example.com"))
    for name in ("HTTPS", "HSTS", "Content-Security-Policy", "Clickjacking protection",
                 "X-Content-Type-Options", "Referrer-Policy", "Permissions-Policy"):
        assert res[name].passed, f"{name} should pass"


def test_security_all_missing(monkeypatch):
    monkeypatch.setattr(sh, "fetch", _resp({}))
    res = _by_name(sh.run_security_headers_audit("https://example.com"))
    assert not res["HSTS"].passed
    assert not res["Content-Security-Policy"].passed
    assert not res["Clickjacking protection"].passed
    assert not res["X-Content-Type-Options"].passed


def test_clickjacking_via_csp_frame_ancestors(monkeypatch):
    monkeypatch.setattr(sh, "fetch", _resp({"content-security-policy": "frame-ancestors 'none'"}))
    res = _by_name(sh.run_security_headers_audit("https://example.com"))
    assert res["Clickjacking protection"].passed


def test_security_not_https_is_critical(monkeypatch):
    monkeypatch.setattr(sh, "fetch", _resp({}, final_url="http://example.com"))
    res = _by_name(sh.run_security_headers_audit("http://example.com"))
    assert not res["HTTPS"].passed
    assert res["HTTPS"].severity == Severity.CRITICAL
    assert "HSTS" not in res  # not checked when not HTTPS


def test_http_upgraded_to_https_passes(monkeypatch):
    monkeypatch.setattr(sh, "fetch", _resp({}, final_url="https://example.com"))
    res = _by_name(sh.run_security_headers_audit("http://example.com"))
    assert res["HTTPS upgrade"].passed


def test_no_headers_captured_is_skipped_not_all_failed(monkeypatch):
    # Reached the site but captured zero headers => measurement gap, not an
    # insecure site. Must skip (one result), not emit a sweep of false FAILs.
    monkeypatch.setattr(sh, "fetch", lambda url, *a, **k: HttpResult("https://example.com", 200, {}, ""))
    res = sh.run_security_headers_audit("https://example.com")
    assert len(res) == 1
    assert res[0].skipped is True
    assert "no response" in res[0].detail.lower()


def test_security_fetch_failure_is_skipped_not_failed(monkeypatch):
    # When neither raw nor browser fetch can reach the site, it's a transport
    # dead-end, not an insecure site — SKIPPED (score-neutral), never failed.
    monkeypatch.setattr(sh, "fetch", lambda url, *a, **k: HttpResult(url, 0, error="conn refused"))
    res = sh.run_security_headers_audit("https://example.com")
    assert len(res) == 1
    assert res[0].skipped is True
    assert not res[0].passed
    assert "not assessed" in res[0].detail.lower()
