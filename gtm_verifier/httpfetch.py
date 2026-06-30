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
from urllib.request import Request, urlopen

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


def fetch(url: str, timeout: float = 15.0, method: str = "GET") -> HttpResult:
    """GET (or HEAD) a URL, following redirects. Never raises — failures are
    returned as an HttpResult with `error` set and status 0."""
    req = Request(
        url,
        headers={"User-Agent": _UA, "Accept-Encoding": "gzip, identity", "Accept": "*/*"},
        method=method,
    )
    try:
        resp = urlopen(req, timeout=timeout, context=ssl.create_default_context())
    except HTTPError as exc:  # 4xx/5xx — still a response with headers + body
        resp = exc
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
