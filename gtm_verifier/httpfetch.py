"""
Minimal stdlib HTTP fetch helper shared by the pure-HTTP audits (SEO, security
headers). Follows redirects, decodes gzip, and still returns headers/body on
4xx/5xx so audits can inspect error responses. No third-party dependencies.
"""

import gzip
import ssl
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import net_guard

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class HttpResult:
    final_url: str
    status: int
    headers: Dict[str, str] = field(default_factory=dict)  # keys lower-cased
    text: str = ""
    error: Optional[str] = None
    source: str = "raw HTTP"  # how the response was obtained (provenance)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status != 0


class _GuardedRedirectHandler(HTTPRedirectHandler):
    """Re-check every redirect hop against the SSRF guard.

    Checking only the URL the caller passed is not enough: a public host can
    302 straight to 169.254.169.254, and urllib would follow it happily.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        net_guard.assert_allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, timeout: float = 15.0, method: str = "GET") -> HttpResult:
    """GET (or HEAD) a URL, following redirects. Never raises — failures are
    returned as an HttpResult with `error` set and status 0.

    Targets that resolve to private/loopback/link-local addresses are refused
    outright (net_guard), on the original URL and on every redirect hop.
    """
    blocked = net_guard.check_url(url)
    if blocked is not None:
        return HttpResult(url, 0, error=f"blocked: {blocked}")

    req = Request(
        url,
        headers={"User-Agent": _UA, "Accept-Encoding": "gzip, identity", "Accept": "*/*"},
        method=method,
    )
    # Explicit verifying TLS context — same as the previous urlopen(context=…).
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()),
                          _GuardedRedirectHandler)
    try:
        resp = opener.open(req, timeout=timeout)
    except HTTPError as exc:  # 4xx/5xx — still a response with headers + body
        resp = exc
    except net_guard.BlockedTargetError as exc:  # redirect pointed somewhere internal
        return HttpResult(url, 0, error=f"blocked: {exc}")
    except URLError as exc:
        return HttpResult(url, 0, error=str(exc.reason))
    except Exception as exc:  # noqa: BLE001 — surface any transport error as data
        return HttpResult(url, 0, error=str(exc))

    raw = resp.read()
    headers = {k.lower(): v for k, v in resp.headers.items()}
    if headers.get("content-encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:  # noqa: BLE001 — fall back to raw bytes
            pass
    text = raw.decode("utf-8", errors="replace")
    final_url = resp.geturl() if hasattr(resp, "geturl") else url
    status = getattr(resp, "status", None) or getattr(resp, "code", 0) or 0
    return HttpResult(final_url, int(status), headers, text)
