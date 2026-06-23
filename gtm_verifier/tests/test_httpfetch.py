"""Unit tests for the stdlib HTTP fetch helper. urlopen is monkeypatched so no
network is touched."""

from urllib.error import URLError

import httpfetch
from httpfetch import HttpResult, fetch


def test_ok_property():
    assert HttpResult("u", 200, {}, "x").ok
    assert not HttpResult("u", 0, error="boom").ok


def test_fetch_urlerror_returns_error(monkeypatch):
    def boom(*a, **k):
        raise URLError("dns fail")

    monkeypatch.setattr(httpfetch, "urlopen", boom)
    r = fetch("https://nope.invalid")
    assert not r.ok
    assert r.status == 0
    assert "dns fail" in r.error


class _FakeHeaders:
    def __init__(self, d):
        self._d = d

    def items(self):
        return self._d.items()

    def get(self, k, default=None):
        return self._d.get(k, default)


class _FakeResp:
    status = 200

    def __init__(self, body=b"<html>hi</html>", headers=None, url="https://example.com/final"):
        self.headers = _FakeHeaders(headers or {"Content-Type": "text/html", "X-Test": "1"})
        self._body = body
        self._url = url

    def read(self):
        return self._body

    def geturl(self):
        return self._url


def test_fetch_success_lowercases_headers(monkeypatch):
    monkeypatch.setattr(httpfetch, "urlopen", lambda *a, **k: _FakeResp())
    r = fetch("https://example.com")
    assert r.ok and r.status == 200
    assert r.headers["content-type"] == "text/html"  # keys lower-cased
    assert r.headers["x-test"] == "1"
    assert r.text == "<html>hi</html>"
    assert r.final_url == "https://example.com/final"


def test_fetch_decodes_gzip(monkeypatch):
    import gzip

    body = gzip.compress(b"hello gzipped")
    resp = _FakeResp(body=body, headers={"Content-Encoding": "gzip"})
    monkeypatch.setattr(httpfetch, "urlopen", lambda *a, **k: resp)
    r = fetch("https://example.com")
    assert r.text == "hello gzipped"
