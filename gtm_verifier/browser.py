"""Browser lifecycle and network capture (Playwright).

One Chromium process is shared by every audit inside a `browser_session()`
block (a CLI run, or one web-app audit request); each audit gets its own
fresh, isolated BrowserContext via `audit_page()` — empty cookies and
sessionStorage, exactly the isolation a fresh Chrome gave under Selenium.

The Playwright sync API is thread-affine: a session must be opened, used and
closed on the same thread. The web app opens one inside each request thread;
never share a session across threads.
"""

import contextlib
import os
import threading
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlparse

from playwright.sync_api import Browser, Page, sync_playwright

import config
from core import _DL_RECORDER_JS

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]

# Masks the webdriver flag so GTM consent/detection code behaves normally.
_WEBDRIVER_MASK_JS = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
)

# Per-thread "current session" so audit functions stay callable without
# threading a browser handle through every signature.
_session = threading.local()


def _launch(pw) -> Browser:
    kwargs = dict(
        # Run without a visible window only when explicitly requested (e.g. on
        # a server). Defaults to a visible browser so runs can be watched.
        headless=os.environ.get("HEADLESS") == "1",
        args=_LAUNCH_ARGS,
        ignore_default_args=["--enable-automation"],
    )
    # Optional escape hatch: BROWSER_CHANNEL=chrome drives installed Google
    # Chrome instead of bundled Chromium (WAF/bot-detection behaviour differs).
    channel = os.environ.get("BROWSER_CHANNEL")
    if channel:
        kwargs["channel"] = channel
    return pw.chromium.launch(**kwargs)


@contextlib.contextmanager
def browser_session():
    """Share one Chromium process across every audit_page() in the block."""
    pw = sync_playwright().start()
    browser = _launch(pw)
    _session.browser = browser
    try:
        yield browser
    finally:
        _session.browser = None
        try:
            browser.close()
        finally:
            pw.stop()


@contextlib.contextmanager
def audit_page(capture_network: bool = False):
    """Yield (page, recorder) in a fresh isolated context.

    Inside a browser_session() the shared browser is reused; otherwise a
    one-shot browser is launched and closed with the context, so audit
    functions remain callable standalone. recorder is None unless
    capture_network is set.
    """
    browser = getattr(_session, "browser", None)
    own_pw = None
    if browser is None or not browser.is_connected():
        own_pw = sync_playwright().start()
        browser = _launch(own_pw)
    context = browser.new_context(
        user_agent=_UA,
        viewport={"width": 1920, "height": 1080},
    )
    # Don't let a slow/hostile site hang an audit indefinitely — fail fast.
    context.set_default_navigation_timeout(45_000)
    context.set_default_timeout(config.DEFAULT_TIMEOUT * 1000)
    context.add_init_script(_WEBDRIVER_MASK_JS)
    # Install the persistent dataLayer recorder on every document
    context.add_init_script(_DL_RECORDER_JS)
    page = context.new_page()
    recorder = NetworkRecorder(page) if capture_network else None
    try:
        yield page, recorder
    finally:
        try:
            context.close()
        finally:
            if own_pw is not None:
                try:
                    browser.close()
                finally:
                    own_pw.stop()


# ── Network capture ───────────────────────────────────────────────────────────

class NetworkRecorder:
    """Accumulates request/response events for one page.

    Replaces the destructive CDP performance-log drains: events append
    monotonically, and drain_collect() uses an internal cursor to return only
    what arrived since the previous drain (the semantics consent.py's
    pre/post-consent split relies on).

    Handlers only run while the owning thread is inside a Playwright call, so
    every wait between navigation and reading must be page.wait_for_timeout(),
    never time.sleep().
    """

    def __init__(self, page: Page):
        self._page = page
        self._requests: List[dict] = []       # {"url", "post_data", "timestamp"}
        self._doc_responses: List[dict] = []  # main-frame document responses
        self._cursor = 0                      # consumed-up-to for drain_collect
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def _on_request(self, request) -> None:
        try:
            post_data = request.post_data or ""
        except Exception:
            post_data = ""  # binary/undecodable bodies
        self._requests.append({
            "url":       request.url,
            "post_data": post_data,
            "timestamp": time.monotonic(),  # ordering only
        })

    def _on_response(self, response) -> None:
        try:
            request = response.request
            if (request.resource_type == "document"
                    and request.frame == self._page.main_frame):
                self._doc_responses.append({
                    "url":     response.url,
                    "status":  response.status,
                    "headers": {k.lower(): v for k, v in response.headers.items()},
                })
        except Exception:
            pass  # page/frame may be gone mid-event

    # ── Consumers ──

    def drain_collect(self, timeout: float = 8.0) -> List[dict]:
        """GA4 collect (v=2) requests seen since the previous drain.

        Polls every 300 ms until at least one appears or timeout expires.
        Returns dicts with keys url / params / timestamp.
        """
        deadline = time.monotonic() + timeout
        while True:
            new = _parse_collect(self._requests[self._cursor:])
            self._cursor = len(self._requests)
            if new or time.monotonic() >= deadline:
                return new
            self._page.wait_for_timeout(300)

    def all_urls(self) -> Set[str]:
        """Every request URL seen so far (does not consume the cursor)."""
        return {r["url"] for r in self._requests}

    def last_document_response(self) -> Optional[dict]:
        """Most recent main-frame document response, or None."""
        return self._doc_responses[-1] if self._doc_responses else None


def _parse_collect(requests: List[dict]) -> List[dict]:
    """Filter raw request records down to GA4 collect (v=2) hits, merging
    query-string and POST-body params (GA4 sends extra data in the body)."""
    entries = []
    for req in requests:
        try:
            url = req["url"]
            if "/collect" not in url:
                continue
            params = dict(parse_qsl(urlparse(url).query))
            if params.get("v") != "2":
                continue
            if req["post_data"]:
                params.update(dict(parse_qsl(req["post_data"])))
            entries.append({
                "url":       url,
                "params":    params,
                "timestamp": req["timestamp"],
            })
        except (KeyError, TypeError):
            continue
    return entries
