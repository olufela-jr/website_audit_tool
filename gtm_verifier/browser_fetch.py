"""
Browser-backed fetch with a raw-HTTP fast path.

The pure-HTTP audits (SEO, security headers) inspect *public* content — page
HTML, meta tags, response headers — that anyone can read. The only catch is
transport: a raw stdlib HTTP client (httpfetch) gets refused by WAF / bot
protection on hardened sites, even though the content is public and a real
browser loads it fine.

`resilient_fetch` therefore tries the cheap raw fetch first and, only if that is
blocked, falls back to an *in-page* `fetch()` executed inside a real Chrome
session. Because that request uses Chrome's own network stack (real TLS
fingerprint, cookies, same-origin context) it passes the checks the raw client
fails, and — being same-origin — it can read the response headers too. Both
paths return the same `HttpResult`, so the audits don't need to care which ran.
"""

import json
from urllib.parse import urlparse

from selenium.common.exceptions import WebDriverException

from core import make_driver
from httpfetch import HttpResult, fetch

# Runs in the page: fetch the URL with the browser's network stack and hand back
# status, (lower-cased) headers, body and final URL. Same-origin responses expose
# all the security/SEO headers we care about (HSTS, CSP, X-Frame-Options, …).
_FETCH_JS = r"""
const url = arguments[0];
const done = arguments[arguments.length - 1];
fetch(url, {credentials: 'include', redirect: 'follow'})
  .then(async (r) => {
    const headers = {};
    r.headers.forEach((v, k) => { headers[k.toLowerCase()] = v; });
    let text = '';
    try { text = await r.text(); } catch (e) {}
    done({status: r.status, headers: headers, text: text, final_url: r.url, error: null});
  })
  .catch((e) => done({status: 0, headers: {}, text: '', final_url: url, error: String(e)}));
"""


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _navigation_response(driver, url: str):
    """Pull the real top-level navigation response (status + headers) from the
    CDP performance log. These are the authoritative document headers — some
    (e.g. HSTS) only appear on the navigation, not on a later in-page fetch.
    Returns (status, headers, final_url) or (0, {}, None) if not captured."""
    try:
        logs = driver.get_log("performance")
    except Exception:  # noqa: BLE001 — perf logging may be off / unsupported
        return 0, {}, None
    best = None
    for entry in logs:
        try:
            msg = json.loads(entry["message"])["message"]
        except (KeyError, json.JSONDecodeError, TypeError):
            continue
        params = msg.get("params", {})
        # The final Document response wins (last one after any redirects).
        if msg.get("method") == "Network.responseReceived" and params.get("type") == "Document":
            best = params.get("response", {})
    if not best:
        return 0, {}, None
    headers = {k.lower(): v for k, v in (best.get("headers") or {}).items()}
    return int(best.get("status") or 0), headers, best.get("url")


def fetch_via_browser(url: str, driver=None, timeout: float = 20.0) -> HttpResult:
    """Fetch `url` from inside a real Chrome session. Launches its own headless
    driver (with perf logging, to read navigation headers) unless one is supplied.
    Never raises — transport failures come back as a not-ok HttpResult."""
    own = driver is None
    if own:
        driver = make_driver(performance_logging=True)
    try:
        driver.set_script_timeout(timeout + 5)
        # Navigate to the target so (a) the in-page fetch below is same-origin and
        # (b) the navigation's real response headers land in the CDP log.
        try:
            driver.get(url)
        except WebDriverException as exc:
            return HttpResult(url, 0, error=f"navigation failed: {exc}")

        nav_status, nav_headers, nav_final = _navigation_response(driver, url)

        # In-page same-origin fetch for the raw body (and as a header fallback).
        data = driver.execute_async_script(_FETCH_JS, url) or {}
        body = data.get("text") or ""
        if not nav_status and data.get("error") and not body:
            return HttpResult(url, 0, error=data.get("error") or "no response")

        # Prefer the authoritative navigation headers; fall back to the fetch's.
        headers = nav_headers or (data.get("headers") or {})
        status = nav_status or int(data.get("status") or 0)
        final_url = nav_final or data.get("final_url") or url
        src = "browser (raw HTTP was blocked)" + ("" if nav_headers else "; headers from in-page fetch")
        return HttpResult(final_url=final_url, status=status, headers=headers, text=body, source=src)
    except WebDriverException as exc:
        return HttpResult(url, 0, error=str(exc))
    finally:
        if own:
            driver.quit()


def resilient_fetch(url: str, timeout: float = 15.0, driver=None) -> HttpResult:
    """Raw HTTP first (fast); on a block/failure, retry via the browser.
    `driver`, if given, is reused for the browser fallback."""
    raw = fetch(url, timeout=timeout)
    if raw.ok:
        return raw
    return fetch_via_browser(url, driver=driver)
