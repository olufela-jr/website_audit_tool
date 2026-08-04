"""
Browser-backed fetch with a raw-HTTP fast path.

The pure-HTTP audits (SEO, security headers) inspect *public* content — page
HTML, meta tags, response headers — that anyone can read. The only catch is
transport: a raw stdlib HTTP client (httpfetch) gets refused by WAF / bot
protection on hardened sites, even though the content is public and a real
browser loads it fine.

`resilient_fetch` therefore tries the cheap raw fetch first and, only if that is
blocked, falls back to an *in-page* `fetch()` executed inside a real browser
session. Because that request uses the browser's own network stack (real TLS
fingerprint, cookies, same-origin context) it passes the checks the raw client
fails, and — being same-origin — it can read the response headers too. Both
paths return the same `HttpResult`, so the audits don't need to care which ran.
"""

from playwright.sync_api import Error as PlaywrightError

import net_guard
from browser import audit_page
from httpfetch import HttpResult, fetch

# Runs in the page: fetch the URL with the browser's network stack and hand back
# status, (lower-cased) headers, body and final URL. Same-origin responses expose
# all the security/SEO headers we care about (HSTS, CSP, X-Frame-Options, …).
_FETCH_JS = r"""
async ([url, timeoutMs]) => {
  try {
    const r = await fetch(url, {
      credentials: 'include',
      redirect: 'follow',
      signal: AbortSignal.timeout(timeoutMs),
    });
    const headers = {};
    r.headers.forEach((v, k) => { headers[k.toLowerCase()] = v; });
    let text = '';
    try { text = await r.text(); } catch (e) {}
    return {status: r.status, headers: headers, text: text, final_url: r.url, error: null};
  } catch (e) {
    return {status: 0, headers: {}, text: '', final_url: url, error: String(e)};
  }
}
"""


def fetch_via_browser(url: str, page=None, timeout: float = 20.0) -> HttpResult:
    """Fetch `url` from inside a real browser session. Opens its own page
    (with network capture, to read navigation headers) unless one is supplied.
    Never raises — transport failures come back as a not-ok HttpResult."""
    if page is not None:
        return _browser_fetch(page, None, url, timeout)
    try:
        with audit_page(capture_network=True) as (own_page, recorder):
            return _browser_fetch(own_page, recorder, url, timeout)
    except PlaywrightError as exc:
        return HttpResult(url, 0, error=str(exc))


def _browser_fetch(page, recorder, url: str, timeout: float) -> HttpResult:
    # The browser path returns the response body to the caller, so it is the
    # one that would turn an internal endpoint into readable data. Guard it
    # even though the raw-HTTP attempt that precedes it was already checked —
    # fetch_via_browser is also called directly.
    blocked = net_guard.check_url(url)
    if blocked is not None:
        return HttpResult(url, 0, error=f"blocked: {blocked}")
    try:
        # Navigate to the target so (a) the in-page fetch below is same-origin
        # and (b) the navigation's real response is available — some headers
        # (e.g. HSTS) only appear on the navigation, not a later in-page fetch.
        try:
            resp = page.goto(url)
        except PlaywrightError as exc:
            return HttpResult(url, 0, error=f"navigation failed: {exc}")

        if resp is None and recorder is not None:
            doc = recorder.last_document_response()
            nav_status = doc["status"] if doc else 0
            nav_headers = doc["headers"] if doc else {}
            nav_final = doc["url"] if doc else None
        elif resp is not None:
            nav_status = resp.status
            nav_headers = {k.lower(): v for k, v in resp.headers.items()}
            nav_final = resp.url
        else:
            nav_status, nav_headers, nav_final = 0, {}, None

        # In-page same-origin fetch for the raw body (and as a header fallback).
        data = page.evaluate(_FETCH_JS, [url, int(timeout * 1000)]) or {}
        body = data.get("text") or ""
        if not nav_status and data.get("error") and not body:
            return HttpResult(url, 0, error=data.get("error") or "no response")

        # Prefer the authoritative navigation headers; fall back to the fetch's.
        headers = nav_headers or (data.get("headers") or {})
        status = nav_status or int(data.get("status") or 0)
        final_url = nav_final or data.get("final_url") or url
        src = "browser (raw HTTP was blocked)" + ("" if nav_headers else "; headers from in-page fetch")
        return HttpResult(final_url=final_url, status=status, headers=headers, text=body, source=src)
    except PlaywrightError as exc:
        return HttpResult(url, 0, error=str(exc))


def resilient_fetch(url: str, timeout: float = 15.0, page=None) -> HttpResult:
    """Raw HTTP first (fast); on a block/failure, retry via the browser.
    `page`, if given, is reused for the browser fallback."""
    raw = fetch(url, timeout=timeout)
    if raw.ok:
        return raw
    return fetch_via_browser(url, page=page)
