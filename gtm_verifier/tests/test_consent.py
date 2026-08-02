"""Unit tests for the consent audit's gcs decoding and the pre-consent GA4
firing classification. No browser is touched — the pure helpers are exercised
directly with hand-built collect-request dicts."""

import pytest

from consent import (
    _CMP_CDN_HOSTS,
    _TCF_CMP_ID_TO_NAME,
    _extract_consent_default,
    _has_gtag_consent_command,
    _match_cdn_host,
    _post_consent_transition_result,
    _pre_consent_firing_result,
)
from network import _interpret_gcs, gcs_analytics_storage_granted


# ── gcs analytics_storage decoding ──────────────────────────────────────────

@pytest.mark.parametrize("gcs, expected", [
    ("G100", False),   # version on, ad denied, analytics denied
    ("G110", False),   # ad granted, analytics denied
    ("G101", True),    # ad denied, analytics granted
    ("G111", True),    # both granted
    ("", None),        # absent
    ("G1", None),      # too short to carry analytics_storage
    ("foo", None),     # not a G-prefixed binary string
    ("G1x0", None),    # non-binary digit
])
def test_gcs_analytics_storage_granted(gcs, expected):
    assert gcs_analytics_storage_granted(gcs) == expected


def test_interpret_gcs_decodes_two_signals_only():
    # The leading digit is a version flag, NOT ad_storage — G100 is all-denied.
    assert _interpret_gcs("G100") == "ad_storage=denied, analytics_storage=denied"
    assert _interpret_gcs("G111") == "ad_storage=granted, analytics_storage=granted"
    assert _interpret_gcs("G110") == "ad_storage=granted, analytics_storage=denied"
    # ad_user_data / ad_personalization are not part of gcs and must not appear.
    assert "ad_user_data" not in _interpret_gcs("G111")


def test_interpret_gcs_returns_raw_on_unparseable():
    assert _interpret_gcs("garbage") == "garbage"


# ── consent-default extraction (both serialisation forms) ───────────────────

_DEFAULTS = {"ad_storage": "denied", "analytics_storage": "denied"}


def test_consent_default_list_form():
    # gtag arguments serialised as a true Array.
    dl = [{"event": "page_view"}, ["consent", "default", _DEFAULTS]]
    assert _extract_consent_default(dl) == _DEFAULTS


def test_consent_default_arguments_object_form():
    # gtag arguments serialised as a dict with string index keys — the case the
    # old list-only check missed on standard gtag implementations.
    dl = [{"0": "consent", "1": "default", "2": _DEFAULTS}]
    assert _extract_consent_default(dl) == _DEFAULTS


def test_consent_default_ignores_consent_update():
    # 'update' is not 'default' — must not be mistaken for the default state.
    dl = [["consent", "update", {"analytics_storage": "granted"}]]
    assert _extract_consent_default(dl) is None


def test_consent_default_ignores_plain_pushes():
    dl = [{"event": "page_view"}, {"ecommerce": {"value": 9.99}}]
    assert _extract_consent_default(dl) is None


def test_consent_default_returns_first_match():
    first = {"ad_storage": "denied"}
    dl = [["consent", "default", first], ["consent", "default", {"ad_storage": "granted"}]]
    assert _extract_consent_default(dl) is first


def test_consent_default_empty_or_none():
    assert _extract_consent_default([]) is None
    assert _extract_consent_default(None) is None


# ── pre-consent firing classification ───────────────────────────────────────

def _req(en, gcs=None):
    params = {"en": en}
    if gcs is not None:
        params["gcs"] = gcs
    return {"url": "https://x/g/collect", "params": params, "timestamp": 0.0}


def test_no_requests_passes():
    result = _pre_consent_firing_result([])
    assert result.passed is True
    assert "No collect requests" in result.detail


def test_cookieless_denied_pings_pass():
    # Consent Mode default-denied pings are compliant, not a violation.
    result = _pre_consent_firing_result([_req("page_view", "G100"), _req("scroll", "G100")])
    assert result.passed is True
    assert "compliant" in result.detail


def test_analytics_granted_before_consent_fails():
    result = _pre_consent_firing_result([_req("page_view", "G111")])
    assert result.passed is False
    assert "page_view" in result.detail


def test_missing_gcs_signal_fails():
    # A collect with no Consent Mode signal at all is unconstrained tracking.
    result = _pre_consent_firing_result([_req("page_view")])
    assert result.passed is False
    assert "absent" in result.detail


def test_mixed_requests_fail_if_any_violation():
    result = _pre_consent_firing_result([_req("a", "G100"), _req("b", "G111")])
    assert result.passed is False
    assert "b" in result.detail
    # Only the violating request is listed in the failure detail.
    assert "1 collect request(s)" in result.detail


# ── post-consent state transition (denied -> granted) ───────────────────────

def test_transition_granted_after_accept_passes():
    pre = [_req("page_view", "G100")]   # denied cookieless ping
    post = [_req("page_view", "G111")]  # granted after accept
    result = _post_consent_transition_result(pre, post, consent_update_found=False)
    assert result.passed is True
    assert "denied → granted" in result.detail


def test_transition_still_denied_after_accept_fails():
    # The masked-broken case: collect requests keep firing post-accept but
    # analytics_storage never flips — the old "any collect = pass" let this slip.
    pre = [_req("page_view", "G100")]
    post = [_req("page_view", "G100"), _req("scroll", "G100")]
    result = _post_consent_transition_result(pre, post, consent_update_found=False)
    assert result.passed is False
    assert "still" in result.detail or "never became granted" in result.detail


def test_transition_consent_event_does_not_rescue_denied_gcs():
    # A consent_update event firing must NOT pass the check if gcs stays denied.
    post = [_req("page_view", "G100")]
    result = _post_consent_transition_result([], post, consent_update_found=True)
    assert result.passed is False
    assert "did not update" in result.detail


def test_transition_event_only_no_collect_passes_weakly():
    # No post-consent collect to inspect, but the CMP pushed a consent_update.
    result = _post_consent_transition_result([], [], consent_update_found=True)
    assert result.passed is True
    assert "no post-consent collect" in result.detail


def test_transition_nothing_happened_fails():
    result = _post_consent_transition_result([], [], consent_update_found=False)
    assert result.passed is False
    assert "CONSENT_ACCEPT_BUTTON" in result.detail


def test_transition_absent_gcs_post_consent_fails():
    # Post-consent collect with no Consent Mode signal at all cannot confirm grant.
    post = [_req("page_view")]  # no gcs
    result = _post_consent_transition_result([], post, consent_update_found=False)
    assert result.passed is False
    assert "absent" in result.detail


# ── tiered CMP detection: pure helpers (Tier 1 / Tier 2) ────────────────────

def test_has_gtag_consent_command_detects_default():
    dl = [{"event": "page_view"}, ["consent", "default", {"analytics_storage": "denied"}]]
    assert _has_gtag_consent_command(dl) is True


def test_has_gtag_consent_command_detects_update():
    # 'update' must count too — _extract_consent_default deliberately ignores
    # it (it's not the default state), but detect_cmp only needs to know a
    # Consent Mode command exists at all.
    dl = [{"0": "consent", "1": "update", "2": {"analytics_storage": "granted"}}]
    assert _has_gtag_consent_command(dl) is True


def test_has_gtag_consent_command_ignores_plain_pushes():
    dl = [{"event": "page_view"}, {"ecommerce": {"value": 9.99}}]
    assert _has_gtag_consent_command(dl) is False


def test_has_gtag_consent_command_empty_or_none():
    assert _has_gtag_consent_command([]) is False
    assert _has_gtag_consent_command(None) is False


def test_tcf_cmp_id_to_name_known_vendors():
    # Spot-check against Google's certified-CMP list (see the comment in
    # consent.py for the source URL) — a wrong id here silently misattributes
    # a vendor, so pin the values we rely on.
    assert _TCF_CMP_ID_TO_NAME[123] == "Iubenda"
    assert _TCF_CMP_ID_TO_NAME[28] == "OneTrust"
    assert _TCF_CMP_ID_TO_NAME[7] == "Didomi"
    assert _TCF_CMP_ID_TO_NAME[5] == "Usercentrics"
    assert _TCF_CMP_ID_TO_NAME[134] == "Cookiebot"


def test_tcf_cmp_id_to_name_unknown_id_degrades_gracefully():
    # An unmapped cmpId must not raise or be silently treated as "no CMP" —
    # the caller reports "present, unnamed", never a false negative.
    assert _TCF_CMP_ID_TO_NAME.get(999999) is None


def test_match_cdn_host_exact_match():
    match = _match_cdn_host({"cs.iubenda.com"})
    assert match == ("cs.iubenda.com", "Iubenda")


def test_match_cdn_host_subdomain_match():
    # A vendor may serve from a subdomain of the known host.
    match = _match_cdn_host({"eu.cdn.cookielaw.org"})
    assert match == ("cdn.cookielaw.org", "OneTrust")


def test_match_cdn_host_no_match():
    assert _match_cdn_host({"example.com", "fonts.googleapis.com"}) is None


def test_match_cdn_host_does_not_match_lookalike_domain():
    # "cs.iubenda.com.evil.com" must not match "cs.iubenda.com" — endswith on
    # the bare label, not a raw substring test.
    assert _match_cdn_host({"cs.iubenda.com.evil.com"}) is None


def test_cmp_cdn_hosts_are_lowercase():
    # _tier2_cdn_hosts lowercases seen hostnames before matching; the lookup
    # table must already be lowercase or matches would silently never fire.
    assert all(host == host.lower() for host in _CMP_CDN_HOSTS)
