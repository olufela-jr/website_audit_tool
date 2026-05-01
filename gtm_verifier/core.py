import json
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config

# ── ANSI colours ──────────────────────────────────────────────────────────────
_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    event: Optional[dict]   # matched dataLayer entry; None if not found or skipped
    passed: bool
    detail: str
    skipped: bool = False


# ── Driver ────────────────────────────────────────────────────────────────────

def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=opts)
    # Mask navigator.webdriver so GTM consent/detection code behaves normally
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


# ── dataLayer helpers ─────────────────────────────────────────────────────────

def get_datalayer_length(driver: webdriver.Chrome) -> int:
    """Snapshot the current length of window.dataLayer."""
    return driver.execute_script("return (window.dataLayer || []).length")


def poll_for_event(
    driver: webdriver.Chrome,
    event_name: str,
    timeout: float,
    after_index: int,
) -> Optional[dict]:
    """
    Poll window.dataLayer every 300 ms until an entry whose 'event' key equals
    event_name appears at or after after_index.  Returns the entry dict or None
    on timeout.  after_index must be captured *before* triggering the action so
    stale earlier pushes are ignored.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data_layer = driver.execute_script("return window.dataLayer || []")
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
        )

    if not required_fields:
        return CheckResult(name=name, event=entry, passed=True, detail="Event found")

    failures = validate_fields(entry, required_fields)
    if failures:
        return CheckResult(
            name=name,
            event=entry,
            passed=False,
            detail="Field validation failed: " + "; ".join(failures),
        )
    return CheckResult(name=name, event=entry, passed=True, detail="All required fields present")


# ── Convenience constructors ──────────────────────────────────────────────────

def skip_check(name: str, reason: str) -> CheckResult:
    return CheckResult(name=name, event=None, passed=False, detail=reason, skipped=True)


def failed_check(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, event=None, passed=False, detail=detail)


# ── Interaction helpers ───────────────────────────────────────────────────────

def accept_consent(driver: webdriver.Chrome) -> None:
    """Click the consent accept button if it appears. Silently skips if absent."""
    try:
        _click(driver, config.CONSENT_ACCEPT_BUTTON)
    except (TimeoutException, NoSuchElementException):
        pass


def _click(driver: webdriver.Chrome, selector: str) -> None:
    """Wait for selector to be clickable then click it."""
    el = WebDriverWait(driver, config.DEFAULT_TIMEOUT).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
    )
    el.click()


def _type(driver: webdriver.Chrome, selector: str, text: str) -> None:
    """Wait for selector to be visible, clear it, then type text."""
    el = WebDriverWait(driver, config.DEFAULT_TIMEOUT).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, selector))
    )
    el.clear()
    el.send_keys(text)


# ── Report printer ────────────────────────────────────────────────────────────

def print_report(journey_results: List[Tuple[str, List[CheckResult]]]) -> int:
    """
    Print a coloured per-journey report.
    Returns 0 if no failures, 1 if any check failed.
    """
    total = passed = failed = skipped = 0

    for journey_name, checks in journey_results:
        print(f"\n{_CYAN}{_BOLD}── {journey_name} ──{_RESET}")
        for r in checks:
            total += 1
            if r.skipped:
                skipped += 1
                print(f"  {_YELLOW}⊘ SKIP{_RESET}  {r.name}  {_DIM}({r.detail}){_RESET}")
            elif r.passed:
                passed += 1
                print(f"  {_GREEN}✓ PASS{_RESET}  {r.name}")
            else:
                failed += 1
                print(f"  {_RED}✗ FAIL{_RESET}  {r.name}  —  {r.detail}")
                if r.event is not None:
                    pretty = json.dumps(r.event, indent=4, default=str)
                    indented = "\n".join("        " + line for line in pretty.splitlines())
                    print(f"{_DIM}        dataLayer entry:\n{indented}{_RESET}")

    status_colour = _GREEN if failed == 0 else _RED
    print(
        f"\n{_BOLD}{status_colour}"
        f"Total: {total}  Passed: {passed}  Failed: {failed}  Skipped: {skipped}"
        f"{_RESET}\n"
    )
    return 1 if failed > 0 else 0
