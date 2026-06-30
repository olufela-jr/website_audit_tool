"""Unit tests for the SEO audit. seo.fetch is monkeypatched to serve canned
HTML / robots.txt / sitemap.xml so no network is touched."""

import seo
from core import Severity
from httpfetch import HttpResult

GOOD_HTML = """<html lang="en"><head>
<title>Example Domain Homepage</title>
<meta name="description" content="A reasonably long meta description that comfortably exceeds fifty characters.">
<link rel="canonical" href="https://example.com/">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:title" content="t">
<meta property="og:description" content="d">
<meta property="og:image" content="https://example.com/i.png">
<script type="application/ld+json">{"@type":"WebSite","name":"x"}</script>
</head><body><h1>Hello</h1></body></html>"""


def _fake_fetch(page_html, robots="User-agent: *\nSitemap: https://example.com/sitemap.xml",
                sitemap="<urlset></urlset>"):
    def fetch(url, *a, **k):
        if url.endswith("robots.txt"):
            return HttpResult(url, 200, {}, robots)
        if url.endswith("sitemap.xml"):
            return HttpResult(url, 200, {}, sitemap)
        return HttpResult(url, 200, {}, page_html)
    return fetch


def _by_name(results):
    return {r.name: r for r in results}


def test_seo_all_good(monkeypatch):
    monkeypatch.setattr(seo, "fetch", _fake_fetch(GOOD_HTML))
    res = _by_name(seo.run_seo_audit("https://example.com"))
    for name in ("Title tag", "Meta description", "Indexable", "Canonical link",
                 "Mobile viewport", "H1 heading", "Open Graph", "robots.txt", "sitemap.xml"):
        assert res[name].passed, f"{name} should pass"
    assert "WebSite" in res["Structured data (JSON-LD)"].detail


def test_seo_missing_title_and_noindex(monkeypatch):
    html = '<html><head><meta name="robots" content="noindex"></head><body></body></html>'
    monkeypatch.setattr(seo, "fetch", _fake_fetch(html, robots="", sitemap=""))
    res = _by_name(seo.run_seo_audit("https://example.com"))
    assert not res["Title tag"].passed
    assert not res["Indexable"].passed
    assert res["Indexable"].severity == Severity.CRITICAL
    assert not res["Canonical link"].passed
    assert not res["robots.txt"].passed
    assert not res["sitemap.xml"].passed


def test_seo_multiple_h1_flagged(monkeypatch):
    html = "<html><head><title>aaaaaaaaaa</title></head><body><h1>a</h1><h1>b</h1></body></html>"
    monkeypatch.setattr(seo, "fetch", _fake_fetch(html))
    res = _by_name(seo.run_seo_audit("https://example.com"))
    assert not res["H1 heading"].passed
    assert "2" in res["H1 heading"].detail


def test_seo_fetch_failure_is_skipped_not_failed(monkeypatch):
    # When neither raw nor browser fetch can retrieve the page, it's a transport
    # dead-end, not a site defect — SKIPPED (score-neutral), never failed.
    monkeypatch.setattr(seo, "fetch", lambda url, *a, **k: HttpResult(url, 0, error="timeout"))
    res = seo.run_seo_audit("https://example.com")
    assert len(res) == 1
    assert res[0].skipped is True
    assert not res[0].passed
    assert "not assessed" in res[0].detail.lower()
