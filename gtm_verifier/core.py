import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config

_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

_SEVERITY_COLOUR = {
    "CRITICAL": "\033[91m\033[1m",  # bold red
    "HIGH":     "\033[91m",         # red
    "MEDIUM":   "\033[93m",         # yellow
    "LOW":      "\033[2m",          # dim
    "INFO":     "\033[96m",         # blue
}


# ── Severity ──────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"
    INFO     = "INFO"


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name:     str
    event:    Optional[dict]    # matched dataLayer entry; None if not found or skipped
    passed:   bool
    detail:   str
    skipped:  bool     = False
    severity: Severity = Severity.HIGH


# ── Persistent dataLayer recorder ─────────────────────────────────────────────
#
# window.dataLayer is wiped on every full-page navigation, so events pushed by
# onclick handlers just before the browser navigates (add_to_cart, generate_lead
# on server-rendered sites) vanish before a post-navigation poll can see them.
#
# This script is injected into every new document *before any page script runs*.
# It intercepts window.dataLayer assignment and .push(), and mirrors a JSON
# snapshot of every entry into sessionStorage, which survives same-origin
# navigations within the tab. Polling reads the mirror, so the event log is
# continuous across the whole journey. (A cross-origin hop starts a fresh log —
# indexes captured before the hop simply match nothing, they never mis-match.)
#
# GTM replaces dataLayer.push with its own processor after it loads; the
# accessor-property trap below keeps recording through that swap.
_DL_RECORDER_JS = r"""
(() => {
  if (window.__dlvInstalled) return;
  try { Object.defineProperty(window, '__dlvInstalled', {value: true}); } catch (e) {}
  const KEY = '__dlv_log';
  const CAP = 5000;              // keep the log bounded (sessionStorage quota)
  const seen = new WeakSet();    // dedupe when the array is replaced/trimmed (same entry refs)
  const mem = [];                // entries recorded on THIS page; flushed to storage on pagehide

  function stored() {
    try { return JSON.parse(sessionStorage.getItem(KEY) || '[]'); }
    catch (e) { return []; }
  }
  function flush() {
    try {
      const log = stored().concat(mem).slice(-CAP);
      sessionStorage.setItem(KEY, JSON.stringify(log));
      mem.length = 0;
    } catch (e) {}
  }
  function toPlain(entry) {
    try { return JSON.parse(JSON.stringify(entry)); }
    catch (e) {
      // Arguments objects / circular refs: best-effort per-key copy
      const out = {};
      try {
        for (const k in entry) {
          try { out[k] = JSON.parse(JSON.stringify(entry[k])); }
          catch (_) { out[k] = String(entry[k]); }
        }
      } catch (_) { return null; }
      return out;
    }
  }
  function record(entry) {
    if (entry !== null && typeof entry === 'object') {
      if (seen.has(entry)) return;   // already recorded (array was replaced, not re-pushed)
      try { seen.add(entry); } catch (e) {}
    }
    mem.push(toPlain(entry));
  }
  function hook(arr) {
    if (!Array.isArray(arr) || arr.__dlvHooked) return arr;
    try { Object.defineProperty(arr, '__dlvHooked', {value: true}); } catch (e) { return arr; }
    arr.forEach(record);  // entries already present (array literal / replaced array)
    // Chain-wrap push as a PLAIN property. GTM later captures this function as
    // its "original push" and replaces arr.push with its own — a clean one-way
    // chain (page → GTM → us → Array.prototype.push). An accessor trap here
    // makes that chain self-referential and sends GTM's queue into a loop.
    const orig = arr.push;
    arr.push = function () {
      for (let i = 0; i < arguments.length; i++) record(arguments[i]);
      return orig.apply(this, arguments);
    };
    return arr;
  }
  let current = undefined;
  try {
    Object.defineProperty(window, 'dataLayer', {
      configurable: true,
      get() { return current; },
      set(v) { current = hook(v); },
    });
  } catch (e) {}
  addEventListener('pagehide', flush);   // persist just before navigating away
  window.__dlvRead = () => stored().concat(mem);
})();
"""

# Falls back to the live dataLayer on documents created before the recorder
# was installed (e.g. the initial about:blank).
_DL_READ_JS = (
    "return (typeof window.__dlvRead === 'function')"
    " ? window.__dlvRead() : (window.dataLayer || [])"
)


# ── Driver ────────────────────────────────────────────────────────────────────

def make_driver(performance_logging: bool = False) -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # Run without a visible window only when explicitly requested (e.g. on a
    # server). Defaults to a visible browser so runs can be watched locally.
    if os.environ.get("HEADLESS") == "1":
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # Performance logging must be set last to avoid capability conflicts
    if performance_logging:
        opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=opts)
    # Don't let a slow/hostile site hang an audit indefinitely — fail fast.
    # Raises TimeoutException (a WebDriverException) from driver.get(), which the
    # audits already handle.
    driver.set_page_load_timeout(45)
    # Mask navigator.webdriver so GTM consent/detection code behaves normally
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    # Install the persistent dataLayer recorder on every document
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument", {"source": _DL_RECORDER_JS}
    )
    return driver


# ── dataLayer helpers ─────────────────────────────────────────────────────────
#
# Both helpers read the persistent recorder log (see _DL_RECORDER_JS), not the
# live window.dataLayer. The log grows monotonically across same-origin page
# loads, so an index captured before a click stays valid even when that click
# triggers a full navigation.

def get_datalayer_length(driver: webdriver.Chrome) -> int:
    try:
        return len(driver.execute_script(_DL_READ_JS) or [])
    except WebDriverException:
        return 0


def poll_for_event(
    driver: webdriver.Chrome,
    event_name: str,
    timeout: float,
    after_index: int,
) -> Optional[dict]:
    """
    Poll the recorded dataLayer log every 300 ms until an entry whose 'event'
    key equals event_name appears at or after after_index.  Returns the entry
    dict or None on timeout.  after_index must be captured *before* triggering
    the action so stale earlier pushes are ignored.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data_layer = driver.execute_script(_DL_READ_JS) or []
        except WebDriverException:
            data_layer = []
        for entry in data_layer[after_index:]:
            if isinstance(entry, dict) and entry.get("event") == event_name:
                return entry
        time.sleep(0.3)
    return None


# ── Field validation ──────────────────────────────────────────────────────────

def validate_fields(event_entry: dict, required_fields: List[str]) -> List[str]:
    """
    Resolve dot-notation paths (e.g. "ecommerce.items.0.item_id") against
    event_entry.  Returns a list of failure messages; empty means all pass.
    """
    failures = []
    for path in required_fields:
        parts = path.split(".")
        value = event_entry
        error: Optional[str] = None
        for part in parts:
            try:
                if isinstance(value, (list, tuple)):
                    value = value[int(part)]
                elif isinstance(value, dict):
                    value = value[part]
                else:
                    error = f"cannot traverse into {type(value).__name__} at '{part}'"
                    break
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                error = str(exc)
                break
        if error:
            failures.append(f"'{path}' not accessible: {error}")
        # Note: only None / "" / [] count as empty. A literal 0 or False is a
        # valid value (e.g. ecommerce value of 0), so those deliberately pass.
        elif value is None or value == "" or value == []:
            failures.append(f"'{path}' is empty (got {value!r})")
    return failures


# ── Combined check ────────────────────────────────────────────────────────────

def check_event(
    driver: webdriver.Chrome,
    event_name: str,
    required_fields: List[str],
    after_index: int,
    check_name: Optional[str] = None,
    timeout: Optional[float] = None,
    severity: Severity = Severity.HIGH,
) -> CheckResult:
    """Poll for event_name then validate required_fields. Returns a CheckResult."""
    name = check_name or event_name
    poll_timeout = timeout if timeout is not None else config.EVENT_POLL_TIMEOUT
    entry = poll_for_event(driver, event_name, poll_timeout, after_index)

    if entry is None:
        return CheckResult(
            name=name,
            event=None,
            passed=False,
            detail=f"Event '{event_name}' not found in dataLayer within {poll_timeout}s",
            severity=severity,
        )

    if not required_fields:
        return CheckResult(
            name=name, event=entry, passed=True,
            detail="Event found", severity=severity,
        )

    failures = validate_fields(entry, required_fields)
    if failures:
        return CheckResult(
            name=name,
            event=entry,
            passed=False,
            detail="Field validation failed: " + "; ".join(failures),
            severity=severity,
        )
    return CheckResult(
        name=name, event=entry, passed=True,
        detail="All required fields present", severity=severity,
    )


# ── Convenience constructors ──────────────────────────────────────────────────

def skip_check(name: str, reason: str, severity: Severity = Severity.HIGH) -> CheckResult:
    return CheckResult(
        name=name, event=None, passed=False,
        detail=reason, skipped=True, severity=severity,
    )


def failed_check(name: str, detail: str, severity: Severity = Severity.HIGH) -> CheckResult:
    return CheckResult(name=name, event=None, passed=False, detail=detail, severity=severity)


# ── Interaction helpers ───────────────────────────────────────────────────────

# Common CMP "accept all" controls, tried after the site-specific selector.
_CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler",                            # OneTrust
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",  # Cookiebot
    "#CybotCookiebotDialogBodyButtonAccept",                   # Cookiebot (older)
    ".qc-cmp2-summary-buttons button[mode='primary']",         # Quantcast
    "#didomi-notice-agree-button",                             # Didomi
    "button.fc-cta-consent",                                   # Google Funding Choices
    "[aria-label='Accept all']", "[aria-label='Allow all']",
]
# Visible button/link text (lower-cased) signalling an accept-all control.
_CONSENT_TEXTS = [
    "accept all", "allow all", "accept cookies", "allow cookies",
    "accept and continue", "yes, i'm happy", "i'm happy",
    "i accept", "agree and close", "yes, i agree", "agree", "got it",
    "accept", "allow",
]
# One in-page sweep: try each selector, then match visible control text.
# Returns the selector/label that was clicked, or null if nothing matched.
_ACCEPT_JS = r"""
const selectors = arguments[0], texts = arguments[1];
function clickable(el){
  if(!el || el.disabled) return false;
  const s = getComputedStyle(el);
  return s.display!=='none' && s.visibility!=='hidden' && el.offsetParent!==null;
}
for (const sel of selectors){
  try { const el = document.querySelector(sel); if(clickable(el)){ el.click(); return sel; } } catch(e){}
}
const els = document.querySelectorAll("button, a, [role='button'], input[type='button'], input[type='submit']");
for (const el of els){
  const label = (el.innerText || el.value || '').trim().toLowerCase();
  if(!label || label.length > 40 || !clickable(el)) continue;
  if(texts.some(t => label === t || label.startsWith(t))){ el.click(); return label; }
}
return null;
"""


def accept_consent(driver: webdriver.Chrome) -> None:
    """Best-effort dismissal of a cookie/consent banner by clicking an
    'accept all' control. Tries the site-specific selector from config first,
    then common CMP selectors, then visible button text — in the top document
    and inside any consent iframes (Sourcepoint, TrustArc, etc.). Polls until
    something is clicked or the timeout elapses, then silently gives up."""
    selectors = [s for s in [config.CONSENT_ACCEPT_BUTTON, *_CONSENT_SELECTORS] if s]
    deadline = time.monotonic() + (config.DEFAULT_TIMEOUT or 10)
    while time.monotonic() < deadline:
        if _sweep_all_frames(driver, selectors):
            return
        time.sleep(0.3)


def _sweep(driver: webdriver.Chrome, selectors: List[str]) -> bool:
    """Run one accept-control sweep in the current browsing context."""
    try:
        return bool(driver.execute_script(_ACCEPT_JS, selectors, _CONSENT_TEXTS))
    except WebDriverException:
        return False


def _sweep_all_frames(driver: webdriver.Chrome, selectors: List[str]) -> bool:
    """Sweep the top document, then any iframes (CMP-looking ones first).
    Selenium can enter cross-origin frames that page JS cannot reach."""
    if _sweep(driver, selectors):
        return True
    for frame in _candidate_frames(driver):
        hit = False
        try:
            driver.switch_to.frame(frame)
            hit = _sweep(driver, selectors)
        except WebDriverException:
            hit = False
        finally:
            try:
                driver.switch_to.default_content()
            except WebDriverException:
                pass
        if hit:
            return True
    return False


_CMP_FRAME_HINTS = (
    "consent", "privacy", "cmp", "gdpr", "onetrust", "sp_message",
    "cookie", "didomi", "trustarc", "sourcepoint",
)


def _candidate_frames(driver: webdriver.Chrome) -> List:
    """Return up to 12 iframes, CMP-looking ones first, to limit frame switching."""
    try:
        frames = driver.find_elements(By.TAG_NAME, "iframe")
    except WebDriverException:
        return []
    scored = []
    for fr in frames:
        try:
            attrs = " ".join(
                a for a in (fr.get_attribute("id"), fr.get_attribute("title"),
                            fr.get_attribute("src")) if a
            ).lower()
        except WebDriverException:
            attrs = ""
        cmpish = any(hint in attrs for hint in _CMP_FRAME_HINTS)
        scored.append((0 if cmpish else 1, fr))
    scored.sort(key=lambda pair: pair[0])
    return [fr for _, fr in scored[:12]]


def _click(driver: webdriver.Chrome, selector: str) -> None:
    el = WebDriverWait(driver, config.DEFAULT_TIMEOUT).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
    )
    el.click()


def _type(driver: webdriver.Chrome, selector: str, text: str) -> None:
    el = WebDriverWait(driver, config.DEFAULT_TIMEOUT).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, selector))
    )
    el.clear()
    el.send_keys(text)


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_checks(checks: List[CheckResult]) -> Tuple[int, int, float]:
    """
    Equal-weighted score for a set of checks: PASS=1, FAIL=0.
    SKIP and INFO checks are excluded from the denominator (they are not
    pass/fail gates). Returns (passed, scorable_total, percent).
    """
    scorable = [c for c in checks if not c.skipped and c.severity != Severity.INFO]
    passed = sum(1 for c in scorable if c.passed)
    total = len(scorable)
    pct = (passed / total * 100.0) if total else 0.0
    return passed, total, pct


def _score_colour(pct: float) -> str:
    if pct >= 80:
        return _GREEN
    if pct >= 50:
        return _YELLOW
    return _RED


# ── Report printer ────────────────────────────────────────────────────────────

def print_report(journey_results: List[Tuple[str, List[CheckResult]]]) -> int:
    """
    Print a coloured per-journey report with an equal-weighted score per audit
    and an overall score. Returns 0 if no failures, 1 if any check failed
    (excluding INFO).
    """
    total = passed = failed = skipped = 0
    overall_passed = overall_scorable = 0

    for journey_name, checks in journey_results:
        print(f"\n{_CYAN}{_BOLD}── {journey_name} ──{_RESET}")
        for r in checks:
            total += 1
            if r.skipped:
                skipped += 1
                sev_col = _SEVERITY_COLOUR.get(r.severity.value, "")
                print(
                    f"  {_YELLOW}⊘ SKIP{_RESET}  "
                    f"{sev_col}[{r.severity.value}]{_RESET}  "
                    f"{r.name}  {_DIM}({r.detail}){_RESET}"
                )
            elif r.passed:
                passed += 1
                if r.severity == Severity.INFO:
                    print(f"  {_CYAN}ℹ INFO{_RESET}  {r.name}  {_DIM}{r.detail}{_RESET}")
                else:
                    print(f"  {_GREEN}✓ PASS{_RESET}  {r.name}")
            else:
                # INFO failures don't count toward exit code
                if r.severity != Severity.INFO:
                    failed += 1
                sev_col = _SEVERITY_COLOUR.get(r.severity.value, "")
                print(
                    f"  {_RED}✗ FAIL{_RESET}  "
                    f"{sev_col}[{r.severity.value}]{_RESET}  "
                    f"{r.name}  —  {r.detail}"
                )
                if r.event is not None:
                    pretty = json.dumps(r.event, indent=4, default=str)
                    indented = "\n".join("        " + line for line in pretty.splitlines())
                    print(f"{_DIM}        dataLayer entry:\n{indented}{_RESET}")

        j_passed, j_total, j_pct = score_checks(checks)
        overall_passed += j_passed
        overall_scorable += j_total
        print(
            f"  {_BOLD}Score: {_score_colour(j_pct)}{j_pct:.0f}%{_RESET}"
            f"{_BOLD} ({j_passed}/{j_total}){_RESET}"
        )

    overall_pct = (overall_passed / overall_scorable * 100.0) if overall_scorable else 0.0
    status_colour = _GREEN if failed == 0 else _RED
    print(
        f"\n{_BOLD}{status_colour}"
        f"Total: {total}  Passed: {passed}  Failed: {failed}  Skipped: {skipped}"
        f"{_RESET}"
    )
    print(
        f"{_BOLD}Overall score: {_score_colour(overall_pct)}{overall_pct:.0f}%{_RESET}"
        f"{_BOLD} ({overall_passed}/{overall_scorable}){_RESET}\n"
    )
    return 1 if failed > 0 else 0
