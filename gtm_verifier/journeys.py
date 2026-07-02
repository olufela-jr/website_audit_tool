"""
Declarative journey engine + wrappers for the site-agnostic audits.

Journeys are defined in the YAML config (see config.example.yaml), not in
Python, so onboarding a new client site never requires code changes. A journey
is a list of steps:

    steps:
      - goto: /shop                  # navigate; relative paths resolve against site.base_url
      - accept_consent: true         # dismiss the CMP banner (best effort, all frames)
      - mark: true                   # manually snapshot the dataLayer position
      - click: ".product-card"      # click the first element matching a CSS selector
      - type: {selector: "input[name='q']", text: "sparkling"}
      - select_index: {selector: "select[name='emirate']", index: 1}
      - expect:                      # assert an event fired since the last action
          event: view_item_list
          require: [ecommerce.items]              # dot-notation required fields
          patterns: {ecommerce.transaction_id: '^PV-\\d+$'}  # regex per field (own check line)
          severity: HIGH             # CRITICAL | HIGH | MEDIUM | LOW | INFO
          name: "listing event"      # optional display name
          timeout: 15                # optional poll override (seconds)
      - skip: {name: sign_up, reason: "Requires post-verification step"}

Index bookkeeping: goto / click / type / select_index snapshot the dataLayer
log position *before* they act, and every following `expect` only matches
events recorded after that point (several expects after one action share it).
accept_consent deliberately does NOT move the marker — page-load events fire
before the banner is dismissed and must stay matchable. Put `mark` before
accept_consent to isolate consent-triggered events.

The dataLayer log persists across same-origin navigations (see core.py), so
events pushed by onclick handlers immediately before a full-page navigation
are still caught.

If an action step fails (bad selector, timeout), every remaining `expect` in
the journey is reported as blocked and the journey stops.
"""

import re
import time
from typing import Any, List, Tuple

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

import config
from analytics import run_analytics_audit
from consent import run_consent_audit
from network import run_network_audit
from seo import run_seo_audit
from security_headers import run_security_headers_audit
from tags_inventory import run_tag_inventory_audit
from core import (
    CheckResult,
    Severity,
    _click,
    _type,
    accept_consent,
    check_event,
    failed_check,
    get_datalayer_length,
    make_driver,
    skip_check,
)

_ACTION_KINDS = ("goto", "accept_consent", "mark", "click", "type", "select_index")
_STEP_KINDS = _ACTION_KINDS + ("expect", "skip")


# ── Spec parsing helpers ──────────────────────────────────────────────────────

def _parse_step(step: Any) -> Tuple[str, Any]:
    """A step is a single-key mapping like {goto: /shop}. Returns (kind, arg)."""
    if not isinstance(step, dict) or len(step) != 1:
        raise ValueError(f"each step must be a single-key mapping, got: {step!r}")
    kind, arg = next(iter(step.items()))
    if kind not in _STEP_KINDS:
        raise ValueError(f"unknown step '{kind}' (valid: {', '.join(_STEP_KINDS)})")
    return kind, arg


def _severity(spec: dict) -> Severity:
    raw = str(spec.get("severity", "HIGH")).upper()
    try:
        return Severity[raw]
    except KeyError:
        return Severity.HIGH


def _absolute(target: str) -> str:
    if str(target).startswith(("http://", "https://")):
        return target
    if not config.BASE_URL:
        raise ValueError(f"relative journey URL '{target}' but no base URL configured")
    return config.BASE_URL + "/" + str(target).lstrip("/")


def _step_desc(kind: str, arg: Any) -> str:
    if isinstance(arg, dict):
        return f"{kind} {arg.get('selector') or arg}"
    return f"{kind} {arg}" if not isinstance(arg, bool) else kind


def _resolve_path(entry: Any, path: str) -> Any:
    """Resolve a dot-notation path (e.g. 'ecommerce.items.0.item_id')."""
    value = entry
    for part in str(path).split("."):
        if isinstance(value, (list, tuple)):
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise KeyError(part)
    return value


# ── Step execution ────────────────────────────────────────────────────────────

def _settle(driver) -> None:
    """Let a click-triggered navigation commit before the next step. Without
    this, a goto right after a form-submitting click can cancel the in-flight
    request (e.g. an add-to-cart POST), leaving the site in the wrong state."""
    time.sleep(0.3)  # give the navigation a beat to start
    deadline = time.monotonic() + config.DEFAULT_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if driver.execute_script("return document.readyState") == "complete":
                return
        except WebDriverException:
            pass  # document mid-swap
        time.sleep(0.2)


def _run_action(driver, kind: str, arg: Any, idx: int) -> int:
    """Perform one action step; returns the new dataLayer marker index."""
    if kind == "goto":
        new_idx = get_datalayer_length(driver)
        driver.get(_absolute(arg))
        return new_idx
    if kind == "accept_consent":
        accept_consent(driver)
        return idx  # deliberately does not move the marker
    if kind == "mark":
        return get_datalayer_length(driver)

    new_idx = get_datalayer_length(driver)
    if kind == "click":
        _click(driver, str(arg))
        _settle(driver)
    elif kind == "type":
        _type(driver, str(arg["selector"]), str(arg.get("text", "")))
    elif kind == "select_index":
        el = WebDriverWait(driver, config.DEFAULT_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, str(arg["selector"])))
        )
        Select(el).select_by_index(int(arg.get("index", 1)))
    return new_idx


def _run_expect(driver, spec: dict, idx: int) -> List[CheckResult]:
    event = spec.get("event")
    if not event:
        return [failed_check("expect", f"journey config error: 'expect' needs an 'event' name, got {spec!r}")]
    severity = _severity(spec)
    result = check_event(
        driver,
        event,
        [str(f) for f in (spec.get("require") or [])],
        idx,
        check_name=spec.get("name"),
        timeout=spec.get("timeout"),
        severity=severity,
    )
    results = [result]
    for path, pattern in (spec.get("patterns") or {}).items():
        results.append(_pattern_check(result, event, str(path), str(pattern), severity))
    return results


def _pattern_check(
    event_result: CheckResult, event: str, path: str, pattern: str, severity: Severity
) -> CheckResult:
    name = f"{event}.{path} format"
    if event_result.event is None:
        return failed_check(name, f"Cannot validate — event '{event}' was not found", severity)
    try:
        value = _resolve_path(event_result.event, path)
    except (KeyError, IndexError, TypeError, ValueError):
        return CheckResult(
            name=name, event=event_result.event, passed=False,
            detail=f"'{path}' not present on the event", severity=severity,
        )
    ok = re.search(pattern, str(value)) is not None
    return CheckResult(
        name=name,
        event=event_result.event,
        passed=ok,
        detail=f"'{path}' = {value!r} {'matches' if ok else 'does not match'} /{pattern}/",
        severity=severity,
    )


# ── Journey runner ────────────────────────────────────────────────────────────

def run_journey(name: str, spec: dict) -> List[CheckResult]:
    """Execute one declarative journey spec and return its check results."""
    steps = (spec or {}).get("steps") or []
    if not steps:
        return [failed_check(name, "journey config error: no 'steps' defined")]

    try:
        parsed = [_parse_step(s) for s in steps]
    except ValueError as exc:
        return [failed_check(name, f"journey config error: {exc}")]

    driver = make_driver()
    results: List[CheckResult] = []
    idx = 0
    try:
        for pos, (kind, arg) in enumerate(parsed):
            if kind == "expect":
                results.extend(_run_expect(driver, arg or {}, idx))
                continue
            if kind == "skip":
                arg = arg or {}
                results.append(skip_check(
                    str(arg.get("name", "unnamed")), str(arg.get("reason", "")), _severity(arg)
                ))
                continue
            try:
                idx = _run_action(driver, kind, arg, idx)
            except (TimeoutException, WebDriverException, ValueError, KeyError) as exc:
                detail = f"step {pos + 1} ({_step_desc(kind, arg)}) failed: {exc}"
                blocked = [a or {} for k, a in parsed[pos + 1:] if k == "expect"]
                if blocked:
                    for e in blocked:
                        results.append(failed_check(
                            e.get("name") or str(e.get("event", "expect")),
                            f"Blocked — {detail}",
                            _severity(e),
                        ))
                else:
                    results.append(failed_check(name, detail))
                break
    finally:
        driver.quit()
    return results


# ── Site-agnostic audits (no selectors / journeys needed) ─────────────────────

def journey_analytics_audit() -> List[CheckResult]:
    return run_analytics_audit(
        config.BASE_URL, expected_gtm_id=config.GTM_ID, expected_ga4_id=config.GA4_ID
    )


def journey_consent_audit() -> List[CheckResult]:
    return run_consent_audit(config.BASE_URL)


def journey_network_audit() -> List[CheckResult]:
    return run_network_audit(config.BASE_URL)


def journey_tag_inventory() -> List[CheckResult]:
    return run_tag_inventory_audit(config.BASE_URL)


def journey_seo() -> List[CheckResult]:
    return run_seo_audit(config.BASE_URL)


def journey_security_headers() -> List[CheckResult]:
    return run_security_headers_audit(config.BASE_URL)
