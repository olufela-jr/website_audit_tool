"""SSRF guard: refuse to point the tool at anything but public internet hosts.

The audits drive a real browser and a raw HTTP client at a URL the operator
types in. Running locally that is harmless. Running as a hosted service it is
not: the same box can reach the cloud metadata endpoint (169.254.169.254),
anything on the VPC, and localhost. A signed-in user could otherwise use the
audit tool as a proxy into infrastructure they cannot reach directly.

So every target is resolved to IP addresses first and rejected if *any* of them
is private, loopback, link-local or otherwise not a public unicast address.
Resolving first is the point — a hostname that anyone can register can point at
127.0.0.1 or the metadata IP, so checking the string alone proves nothing.

Auditing a private address is legitimate when you are running locally against a
staging site, so `ALLOW_PRIVATE_TARGETS=1` disables the guard. It is deliberately
opt-in: absent the variable the guard is ON, which is the safe default for a
deployment that forgot to think about it.

Known gap: this is a check at request time, not a pinned connection. A hostname
whose DNS answer changes between the check and the connection (DNS rebinding)
can still slip through. Closing that needs connection-level pinning in both
urllib and Chromium; see notes/cloud-followups.md #4.
"""

import ipaddress
import os
import socket
from functools import lru_cache
from typing import List, Optional
from urllib.parse import urlparse

# Schemes worth touching at all. file:// and gopher:// are classic SSRF pivots.
_ALLOWED_SCHEMES = {"http", "https"}

# Hostnames that resolve to link-local metadata on the major clouds. Resolution
# below catches these anyway; naming them gives a clearer message.
_BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
    "instance-data",
}


class BlockedTargetError(Exception):
    """Raised when a URL points somewhere the tool must not go."""


def guard_disabled() -> bool:
    """True when the operator has explicitly allowed private targets."""
    return os.environ.get("ALLOW_PRIVATE_TARGETS") == "1"


def _ip_reason(ip: ipaddress._BaseAddress) -> Optional[str]:
    """Why this address is off-limits, or None if it is a public unicast one."""
    # Order matters only for the message: several of these overlap in
    # ipaddress (0.0.0.0 and 169.254.0.0/16 both report is_private too), so
    # test the specific cases first to explain the block accurately.
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        # Covers 169.254.0.0/16 — the cloud metadata range.
        return "link-local address (cloud metadata range)"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    # IPv6 can wrap a v4 address (::ffff:127.0.0.1) and tunnel past a naive
    # check — unwrap and re-test the embedded address.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _ip_reason(mapped)
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None:
        return _ip_reason(sixtofour)
    return None


@lru_cache(maxsize=512)
def _resolve(host: str) -> tuple:
    """All IPs a host resolves to. Cached: a single audit hits the same host
    many times, and this sits in front of every request."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return tuple({info[4][0] for info in infos})


def check_url(url: str) -> Optional[str]:
    """Return None if `url` may be fetched, else a human-readable reason.

    Never raises: a URL that cannot be parsed or resolved is reported as a
    reason string, because failing closed is the safe direction here.
    """
    if guard_disabled():
        return None

    try:
        parsed = urlparse(url)
    except Exception as exc:  # noqa: BLE001 — malformed input is a block reason
        return f"could not parse URL: {exc}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return f"scheme '{scheme or 'none'}' is not allowed (http/https only)"

    host = (parsed.hostname or "").strip().rstrip(".")
    if not host:
        return "URL has no hostname"
    if host.lower() in _BLOCKED_HOSTNAMES:
        return f"'{host}' is a cloud metadata hostname"

    # An IP literal needs no DNS — check it directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        reason = _ip_reason(literal)
        return f"{host} is a {reason}" if reason else None

    try:
        addresses = _resolve(host)
    except socket.gaierror as exc:
        return f"could not resolve '{host}': {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"could not resolve '{host}': {exc}"

    if not addresses:
        return f"'{host}' did not resolve to any address"

    # Every answer must be public: one private A record is enough to pivot.
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return f"'{host}' resolved to an unparseable address {addr!r}"
        reason = _ip_reason(ip)
        if reason is not None:
            return f"'{host}' resolves to {addr}, a {reason}"
    return None


def assert_allowed(url: str) -> None:
    """Raise BlockedTargetError unless `url` is a public http(s) target."""
    reason = check_url(url)
    if reason is not None:
        raise BlockedTargetError(f"Refusing to fetch {url} — {reason}.")


def is_allowed(url: str) -> bool:
    return check_url(url) is None


def blocked_urls(urls: List[str]) -> List[str]:
    """Subset of `urls` that would be blocked — for reporting, not enforcement."""
    return [u for u in urls if check_url(u) is not None]
