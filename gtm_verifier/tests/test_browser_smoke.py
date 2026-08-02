"""Browser smoke tests for the Playwright migration.

Run explicitly with:  HEADLESS=1 pytest -m browser

A local http.server serves tiny static pages that exercise the riskiest ported
pieces: the NetworkRecorder (collect parsing + cursor semantics), the injected
dataLayer recorder surviving same-origin navigation, and the consent sweep in
the top document and inside an iframe. No external network access needed.
"""

import os
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

import pytest

from browser import audit_page, browser_session
from core import accept_consent, get_datalayer_length, poll_for_event

pytestmark = pytest.mark.browser

os.environ.setdefault("HEADLESS", "1")

_PAGE_A = """<!doctype html>
<html><head><title>A</title></head><body>
<a id="to-b" href="/b.html">go to b</a>
<script>
  window.dataLayer = window.dataLayer || [];
  dataLayer.push({event: 'page_view_a', page: 'a'});
  // gtag-style push of an `arguments` object (array-like, not an Array)
  function gtag(){ dataLayer.push(arguments); }
  gtag('consent', 'default', {analytics_storage: 'denied'});
  // GA4-style collect beacon (the recorder must catch sendBeacon traffic)
  navigator.sendBeacon('/collect?v=2&en=test_event&cid=1.1&sid=2&gcs=G111');
</script>
</body></html>"""

_PAGE_B = """<!doctype html>
<html><head><title>B</title></head><body>
<script>
  window.dataLayer = window.dataLayer || [];
  dataLayer.push({event: 'page_view_b', page: 'b'});
</script>
</body></html>"""

_PAGE_CONSENT = """<!doctype html>
<html><head><title>Consent</title></head><body>
<div id="banner"><button id="accept-btn" onclick="dataLayer.push({event:'consent_accepted'})">Accept all</button></div>
<script>window.dataLayer = window.dataLayer || [];</script>
</body></html>"""

_PAGE_CONSENT_IFRAME = """<!doctype html>
<html><head><title>Consent iframe host</title></head><body>
<iframe id="sp_message_iframe_1" src="/consent.html"></iframe>
</body></html>"""

# Stubs the IAB TCF 2.0 API with a real vendor's cmpId (Iubenda = 123, per
# https://support.google.com/adsense/answer/13554116) so detect_cmp's Tier 1
# can be exercised end-to-end, including cmpId -> vendor name resolution.
_PAGE_TCF = """<!doctype html>
<html><head><title>TCF stub</title></head><body>
<script>
window.__tcfapi = function(command, version, callback) {
  if (command === 'ping') {
    callback({cmpId: 123, cmpVersion: 1, gdprApplies: true, cmpStatus: 'loaded'}, true);
  }
};
</script>
</body></html>"""

# No banner at load — injected 1.2s later via a generic selector match. Proves
# detect_cmp's internal poll (not an external wait_for_timeout) catches a CMP
# that races a single post-goto snapshot.
_PAGE_ASYNC_BANNER = """<!doctype html>
<html><head><title>Async banner</title></head><body>
<script>
setTimeout(() => {
  const div = document.createElement('div');
  div.id = 'my-cookie-consent-banner';
  div.textContent = 'We use cookies';
  document.body.appendChild(div);
}, 1200);
</script>
</body></html>"""

_PAGES = {
    "/a.html": _PAGE_A,
    "/b.html": _PAGE_B,
    "/consent.html": _PAGE_CONSENT,
    "/iframe.html": _PAGE_CONSENT_IFRAME,
    "/tcf.html": _PAGE_TCF,
    "/async_banner.html": _PAGE_ASYNC_BANNER,
}


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    def _respond(self, code: int, body: bytes = b"", ctype: str = "text/html"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        page = _PAGES.get(self.path.split("?")[0])
        if page is not None:
            self._respond(200, page.encode())
        else:
            self._respond(404, b"not found")

    def do_POST(self):  # sendBeacon posts to /collect
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._respond(204)


@pytest.fixture(scope="module")
def site():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def shared_browser():
    with browser_session():
        yield


def test_recorder_collect_and_cursor(site, shared_browser):
    with audit_page(capture_network=True) as (page, recorder):
        page.goto(f"{site}/a.html")
        hits = recorder.drain_collect(timeout=5.0)
        assert len(hits) == 1
        params = hits[0]["params"]
        assert params["v"] == "2"
        assert params["en"] == "test_event"
        assert params["cid"] == "1.1"
        assert params["gcs"] == "G111"
        # Cursor semantics: a second drain returns only NEW requests — none.
        assert recorder.drain_collect(timeout=0.5) == []


def test_datalayer_recorder_survives_navigation(site, shared_browser):
    with audit_page() as (page, _):
        page.goto(f"{site}/a.html")
        assert poll_for_event(page, "page_view_a", timeout=5.0, after_index=0)
        idx = get_datalayer_length(page)
        assert idx >= 1
        page.locator("#to-b").click()
        # After a same-origin navigation the log persists: A's events are still
        # there (index survives) and B's new event lands after the marker.
        assert poll_for_event(page, "page_view_b", timeout=5.0, after_index=idx)
        assert poll_for_event(page, "page_view_a", timeout=1.0, after_index=0)


def test_gtag_arguments_object_shape(site, shared_browser):
    from consent import _extract_consent_default

    with audit_page() as (page, _):
        page.goto(f"{site}/a.html")
        poll_for_event(page, "page_view_a", timeout=5.0, after_index=0)
        dl = page.evaluate("window.__dlvRead()")
        # The gtag('consent', 'default', ...) push must be recognisable in the
        # recorded log (arguments objects serialize as {'0': ..., '1': ...}).
        state = _extract_consent_default(dl)
        assert state == {"analytics_storage": "denied"}


def test_accept_consent_top_document(site, shared_browser):
    with audit_page() as (page, _):
        page.goto(f"{site}/consent.html")
        accept_consent(page)
        assert poll_for_event(page, "consent_accepted", timeout=5.0, after_index=0)


def test_accept_consent_inside_iframe(site, shared_browser):
    with audit_page() as (page, _):
        page.goto(f"{site}/iframe.html")
        accept_consent(page)
        clicked = page.frames[1].evaluate(
            "window.dataLayer.some(e => e && e.event === 'consent_accepted')"
        )
        assert clicked


def test_isolated_contexts_between_audits(site, shared_browser):
    with audit_page() as (page, _):
        page.goto(f"{site}/a.html")
        page.evaluate("sessionStorage.setItem('probe', 'leaked')")
    with audit_page() as (page, _):
        page.goto(f"{site}/a.html")
        assert page.evaluate("sessionStorage.getItem('probe')") is None


# ── tiered CMP detection (consent.detect_cmp) ───────────────────────────────

def test_detect_cmp_tier1_tcf_ping_names_vendor(site, shared_browser):
    from consent import detect_cmp

    with audit_page() as (page, _):
        page.goto(f"{site}/tcf.html")
        detection = detect_cmp(page)
        assert detection.present is True
        assert detection.vendor == "Iubenda"
        assert any("cmpId=123" in e for e in detection.evidence)


def test_detect_cmp_tier3_waits_for_async_banner(site, shared_browser):
    from consent import detect_cmp

    with audit_page() as (page, _):
        page.goto(f"{site}/async_banner.html")
        # No external wait_for_timeout here — the banner injects 1.2s after
        # load, so this only passes if detect_cmp polls internally instead of
        # taking a single snapshot right after goto().
        detection = detect_cmp(page)
        assert detection.present is True
        assert detection.banner_visible is True
        assert detection.vendor == "generic"  # matched a generic selector, no named vendor


def test_detect_cmp_absent_reports_present_false(site, shared_browser, monkeypatch):
    import config
    from consent import detect_cmp

    # b.html (not a.html — a.html pushes a gtag consent default that Tier 1
    # would correctly detect) has no CMP signal of any kind. Shrink the poll
    # timeout so the test doesn't have to wait out the full production default.
    monkeypatch.setattr(config, "CMP_DETECTION_TIMEOUT", 0.5)
    with audit_page() as (page, _):
        page.goto(f"{site}/b.html")
        detection = detect_cmp(page)
        assert detection.present is False
        assert detection.banner_visible is False
        assert detection.evidence == []
