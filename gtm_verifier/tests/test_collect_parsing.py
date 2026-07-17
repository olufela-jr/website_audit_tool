"""Unit tests for browser._parse_collect — no browser or network needed."""

from browser import _parse_collect


def _req(url, post=None):
    return {"url": url, "post_data": post, "timestamp": 1.0}


def test_standard_collect_endpoint_matched():
    hits = _parse_collect([
        _req("https://www.google-analytics.com/g/collect?v=2&tid=G-ABC1234&en=page_view"),
    ])
    assert len(hits) == 1
    assert hits[0]["params"]["en"] == "page_view"


def test_sgtm_proxied_endpoint_matched_by_signature():
    # sGTM proxies often rewrite the /collect path (seen live on tails.com:
    # /metrics/ag/g/c) — the v=2 + G- tid combination still identifies GA4.
    hits = _parse_collect([
        _req("https://tails.com/metrics/ag/g/c?v=2&tid=G-LFPWG8JS5M&cid=1.1"),
    ])
    assert len(hits) == 1
    assert hits[0]["params"]["tid"] == "G-LFPWG8JS5M"


def test_non_ga4_requests_ignored():
    hits = _parse_collect([
        _req("https://tails.com/metrics/ag/g/c?v=1&tid=G-ABC1234"),   # wrong protocol version
        _req("https://example.com/api/data?v=2&foo=bar"),             # v=2 but no GA4 signature
        _req("https://example.com/collect"),                          # no params at all
    ])
    assert hits == []


def test_post_body_params_merged():
    hits = _parse_collect([
        _req("https://www.google-analytics.com/g/collect?v=2&tid=G-ABC1234",
             post="en=purchase&cid=1.1"),
    ])
    assert hits[0]["params"]["en"] == "purchase"
    assert hits[0]["params"]["cid"] == "1.1"
