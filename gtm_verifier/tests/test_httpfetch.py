"""Unit tests for the stdlib HTTP fetch helper. The opener is monkeypatched so
no network is touched, and the SSRF guard is stubbed to allow — its own
behaviour is covered in test_net_guard.py."""

import pytest
from urllib.error import URLError

import httpfetch
import net_guard
from httpfetch import HttpResult, fetch


@pytest.fixture(autouse=True)
def _allow_all_targets(monkeypatch):
    """Let every URL through the guard; these tests are about transport.
    Without this the guard would do real DNS for example.com."""
    monkeypatch.setattr(net_guard, "check_url", lambda url: None)


class _FakeOpener:
    """Stands in for the object build_opener returns."""

    def __init__(self, behaviour):
        self._behaviour = behaviour

    def open(self, req, timeout=None):
        return self._behaviour()


def _patch_opener(monkeypatch, behaviour):
    monkeypatch.setattr(httpfetch, "build_opener",
                        lambda *handlers: _FakeOpener(behaviour))


def test_ok_property():
    assert HttpResult("u", 200, {}, "x").ok
    assert not HttpResult("u", 0, error="boom").ok


def test_fetch_urlerror_returns_error(monkeypatch):
    def boom():
        raise URLError("dns fail")

    _patch_opener(monkeypatch, boom)
    r = fetch("https://nope.invalid")
    assert not r.ok
    assert r.status == 0
    assert "dns fail" in r.error


def test_fetch_refuses_blocked_target(monkeypatch):
    """The guard runs before any connection is attempted."""
    monkeypatch.setattr(net_guard, "check_url", lambda url: "loopback address")

    def should_not_run():
        raise AssertionError("opener must not be called for a blocked target")

    _patch_opener(monkeypatch, should_not_run)
    r = fetch("http://127.0.0.1/")
    assert not r.ok
    assert "blocked" in r.error and "loopback" in r.error


def test_fetch_refuses_redirect_to_blocked_target(monkeypatch):
    """A public URL that 302s somewhere internal is refused mid-flight."""
    def redirected():
        raise net_guard.BlockedTargetError(
            "Refusing to fetch http://169.254.169.254/ — link-local address.")

    _patch_opener(monkeypatch, redirected)
    r = fetch("https://example.com")
    assert not r.ok
    assert "blocked" in r.error and "169.254.169.254" in r.error


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
    _patch_opener(monkeypatch, _FakeResp)
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
    _patch_opener(monkeypatch, lambda: resp)
    r = fetch("https://example.com")
    assert r.text == "hello gzipped"
