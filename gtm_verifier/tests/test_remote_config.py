"""Unit tests for the GA4 remote-config audit. remote_config.fetch is
monkeypatched to serve saved gtag.js fixtures so no network is touched.

Fixtures (truncated after the `var data = {...}` blob — all the parser needs):
  gtag_palmview.js  — real property G-JXWM0TX7FS (palm-view demo site)
  gtag_fallback.js  — Google's generic fallback served for a bogus ID
"""

import os

import network
import remote_config
from core import Severity
from httpfetch import HttpResult

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PALMVIEW_ID = "G-JXWM0TX7FS"


def _fixture(name):
    with open(os.path.join(_FIXTURES, name)) as f:
        return f.read()


def _fake_fetch(body_by_url_part):
    """Map a URL substring -> body; unmatched URLs get a 404 empty body."""
    def fetch(url, *a, **k):
        for part, body in body_by_url_part.items():
            if part in url:
                return HttpResult(url, 200, {}, body)
        return HttpResult(url, 404, {}, "")
    return fetch


def _by_name(results):
    return {r.name: r for r in results}


# ── Parser ────────────────────────────────────────────────────────────────────

def test_parse_real_property(monkeypatch):
    monkeypatch.setattr(remote_config, "fetch",
                        _fake_fetch({PALMVIEW_ID: _fixture("gtag_palmview.js")}))
    rc = remote_config.fetch_remote_config(PALMVIEW_ID)
    assert rc.resolved is True
    assert rc.error is None
    assert set(rc.enhanced_measurement) == {
        "page_view", "scroll", "outbound_click", "site_search", "video", "download", "form",
    }
    assert all(rc.enhanced_measurement.values())
    assert rc.key_events == ["purchase"]
    assert rc.google_signals == "ENABLED"
    assert rc.redact_email is True
    assert rc.cross_domain_patterns == []
    assert rc.transport_url is None
    assert rc.regional_settings  # regscope table present on this property


def test_fallback_script_is_not_resolved(monkeypatch):
    monkeypatch.setattr(remote_config, "fetch",
                        _fake_fetch({"G-NOTAREALID1": _fixture("gtag_fallback.js")}))
    rc = remote_config.fetch_remote_config("G-NOTAREALID1")
    # Google returns HTTP 200 for bogus IDs; resolution hinges on the literal ID.
    assert rc.fetch_status == 200
    assert rc.resolved is False


def test_malformed_blob_sets_error_not_exception(monkeypatch):
    truncated = _fixture("gtag_palmview.js")[:2000]  # cut mid-blob
    monkeypatch.setattr(remote_config, "fetch", _fake_fetch({PALMVIEW_ID: truncated}))
    rc = remote_config.fetch_remote_config(PALMVIEW_ID)
    assert rc.resolved is False
    assert rc.error is not None


def test_gtm_value_normalisation():
    assert remote_config._from_gtm_value(["list", "a", "b"]) == ["a", "b"]
    assert remote_config._from_gtm_value(["map", "k", "v", "k2", 2]) == {"k": "v", "k2": 2}
    assert remote_config._from_gtm_value(["list", ["map", "k", "v"]]) == [{"k": "v"}]
    assert remote_config._from_gtm_value("plain") == "plain"


# ── Audit: resolution outcomes ────────────────────────────────────────────────

def test_explicit_unresolved_id_fails(monkeypatch):
    monkeypatch.setattr(remote_config, "fetch",
                        _fake_fetch({"gtag/js": _fixture("gtag_fallback.js")}))
    res = _by_name(remote_config.run_remote_config_audit(measurement_id="G-NOTAREALID1"))
    check = res["Property config resolved"]
    assert not check.passed and not check.skipped
    assert "fallback" in check.detail


def test_discovered_unresolved_id_skips(monkeypatch):
    monkeypatch.setattr(
        remote_config, "discover_measurement_ids",
        lambda url: (["G-STALEID99"], [], "page HTML"),
    )
    monkeypatch.setattr(remote_config, "fetch",
                        _fake_fetch({"gtag/js": _fixture("gtag_fallback.js")}))
    res = _by_name(remote_config.run_remote_config_audit(url="https://example.com"))
    assert res["Property config resolved"].skipped is True


def test_unreachable_network_skips(monkeypatch):
    monkeypatch.setattr(remote_config, "fetch",
                        lambda url, *a, **k: HttpResult(url, 0, error="timeout"))
    res = remote_config.run_remote_config_audit(measurement_id=PALMVIEW_ID)
    skip = [r for r in res if r.skipped]
    assert skip and "not assessed" in skip[0].detail.lower()


def test_no_url_no_id_skips():
    res = remote_config.run_remote_config_audit()
    assert len(res) == 1 and res[0].skipped


# ── Audit: discovery ─────────────────────────────────────────────────────────

def test_gtm_only_discovery_via_container(monkeypatch):
    page_html = "<script>gtm.start</script><!-- GTM-K4BLC735 -->"
    container_js = f'var x = "{PALMVIEW_ID}";'
    import browser_fetch
    monkeypatch.setattr(browser_fetch, "resilient_fetch",
                        lambda url, *a, **k: HttpResult(url, 200, {}, page_html))
    monkeypatch.setattr(remote_config, "fetch", _fake_fetch({
        "gtm.js?id=GTM-K4BLC735": container_js,
        "gtag/js": _fixture("gtag_palmview.js"),
    }))
    res = remote_config.run_remote_config_audit(url="https://example.com")
    by_name = _by_name(res)
    assert PALMVIEW_ID in by_name["Measurement ID discovery"].detail
    assert "gtm.js" in by_name["Measurement ID discovery"].detail
    assert by_name["Property config resolved"].passed


def test_no_ids_at_all_skips(monkeypatch):
    import browser_fetch
    monkeypatch.setattr(browser_fetch, "resilient_fetch",
                        lambda url, *a, **k: HttpResult(url, 200, {}, "<html></html>"))
    res = remote_config.run_remote_config_audit(url="https://example.com")
    assert len(res) == 1 and res[0].skipped


# ── Audit: expectations (client YAML) ────────────────────────────────────────

def _audit_palmview(monkeypatch, **kwargs):
    monkeypatch.setattr(remote_config, "fetch",
                        _fake_fetch({"gtag/js": _fixture("gtag_palmview.js")}))
    return _by_name(remote_config.run_remote_config_audit(
        measurement_id=PALMVIEW_ID, **kwargs))


def test_expected_key_event_present_passes(monkeypatch):
    res = _audit_palmview(monkeypatch, expected={"key_events": ["purchase"]})
    assert res["Expected key events configured"].passed


def test_expected_key_event_missing_fails(monkeypatch):
    res = _audit_palmview(monkeypatch, expected={"key_events": ["generate_lead"]})
    check = res["Expected key events configured"]
    assert not check.passed
    assert "generate_lead" in check.detail
    assert check.severity == Severity.HIGH


def test_expected_signals_and_em(monkeypatch):
    res = _audit_palmview(monkeypatch, expected={
        "google_signals": "ENABLED",
        "enhanced_measurement": {"page_view": True, "form": True},
    })
    assert res["Expected Google Signals state"].passed
    assert res["Expected enhanced measurement"].passed


def test_no_expectations_no_expectation_checks(monkeypatch):
    res = _audit_palmview(monkeypatch)
    assert "Expected key events configured" not in res


# ── Audit: cross-checks vs network audit ─────────────────────────────────────

def test_cross_check_with_network_run(monkeypatch):
    monkeypatch.setattr(remote_config, "fetch",
                        _fake_fetch({"gtag/js": _fixture("gtag_palmview.js")}))
    monkeypatch.setattr(network, "LAST_RUN", {
        "url": "https://example.com",
        "event_names": ["page_view", "purchase"],
        "params": [],
    })
    res = _by_name(remote_config.run_remote_config_audit(
        url="https://example.com", measurement_id=PALMVIEW_ID))
    check = res["Key events vs observed traffic"]
    assert "purchase" in check.detail and "observed on this page load" in check.detail


def test_cross_check_skipped_without_network_run(monkeypatch):
    monkeypatch.setattr(remote_config, "fetch",
                        _fake_fetch({"gtag/js": _fixture("gtag_palmview.js")}))
    monkeypatch.setattr(network, "LAST_RUN", {})
    res = _by_name(remote_config.run_remote_config_audit(
        url="https://example.com", measurement_id=PALMVIEW_ID))
    assert "cross-check skipped" in res["Config vs observed traffic"].detail
