"""Unit tests for the SSRF guard.

DNS is stubbed throughout: these assert the decision logic, not what a given
hostname happens to resolve to today.
"""

import pytest

import net_guard


def _clear_resolver_cache():
    """_resolve is lru_cache-wrapped, but tests monkeypatch it with a plain
    function that has no cache_clear — tolerate both."""
    getattr(net_guard._resolve, "cache_clear", lambda: None)()


@pytest.fixture(autouse=True)
def _guard_enabled(monkeypatch):
    """Default to the guard being ON, regardless of the caller's environment."""
    monkeypatch.delenv("ALLOW_PRIVATE_TARGETS", raising=False)
    _clear_resolver_cache()
    yield
    _clear_resolver_cache()


def _resolves_to(monkeypatch, *addresses):
    monkeypatch.setattr(net_guard, "_resolve", lambda host: tuple(addresses))


# ── IP literals: no DNS involved ──────────────────────────────────────────────

@pytest.mark.parametrize("url,fragment", [
    ("http://169.254.169.254/computeMetadata/v1/", "link-local"),
    ("http://127.0.0.1/", "loopback"),
    ("http://10.0.0.5/", "private"),
    ("http://192.168.1.1/", "private"),
    ("http://172.16.0.1/", "private"),
    ("http://[::1]/", "loopback"),
    ("http://0.0.0.0/", "unspecified"),
])
def test_blocks_private_ip_literals(url, fragment):
    reason = net_guard.check_url(url)
    assert reason is not None and fragment in reason


def test_blocks_ipv4_mapped_ipv6():
    """::ffff:127.0.0.1 must not tunnel past the check."""
    assert net_guard.check_url("http://[::ffff:127.0.0.1]/") is not None


def test_allows_public_ip_literal():
    assert net_guard.check_url("https://8.8.8.8/") is None


# ── Schemes ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://example.com/",
    "ftp://example.com/",
])
def test_blocks_non_http_schemes(url):
    reason = net_guard.check_url(url)
    assert reason is not None and "scheme" in reason


def test_blocks_url_without_hostname():
    assert net_guard.check_url("http://") is not None


# ── Hostname resolution ───────────────────────────────────────────────────────

def test_blocks_hostname_resolving_to_loopback(monkeypatch):
    """A public name pointing inward is the whole reason we resolve first."""
    _resolves_to(monkeypatch, "127.0.0.1")
    reason = net_guard.check_url("https://evil.example.com/")
    assert reason is not None and "loopback" in reason


def test_blocks_when_any_answer_is_private(monkeypatch):
    """One private A record is enough to pivot, so all answers must be public."""
    _resolves_to(monkeypatch, "93.184.216.34", "10.1.2.3")
    assert net_guard.check_url("https://mixed.example.com/") is not None


def test_allows_public_hostname(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    assert net_guard.check_url("https://example.com/") is None


def test_blocks_metadata_hostname():
    reason = net_guard.check_url("http://metadata.google.internal/")
    assert reason is not None and "metadata" in reason


def test_blocks_unresolvable_host(monkeypatch):
    """Fails closed: if we cannot tell where it points, we do not go."""
    import socket

    def boom(host):
        raise socket.gaierror("nope")

    monkeypatch.setattr(net_guard, "_resolve", boom)
    assert net_guard.check_url("https://nope.invalid/") is not None


def test_trailing_dot_host_is_still_checked(monkeypatch):
    """'example.com.' is the same host; the guard must not be fooled by it."""
    _resolves_to(monkeypatch, "127.0.0.1")
    assert net_guard.check_url("https://example.com./") is not None


# ── Escape hatch ──────────────────────────────────────────────────────────────

def test_allow_private_targets_disables_guard(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_TARGETS", "1")
    assert net_guard.check_url("http://127.0.0.1:3000/") is None


def test_assert_allowed_raises_when_blocked():
    with pytest.raises(net_guard.BlockedTargetError):
        net_guard.assert_allowed("http://169.254.169.254/")


def test_is_allowed_mirrors_check_url(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    assert net_guard.is_allowed("https://example.com/")
    assert not net_guard.is_allowed("http://127.0.0.1/")
