"""
Scenario interpreter — the HOW of an audit.

Reads a scenario YAML (site-specific steps) and drives the browser to provoke
events, then asserts them against a loaded Spec. This is a thin spike proving
one data-driven scenario reproduces a hand-written journey from journeys.py.

Run:
    python scenario.py                 # runs product_detail against ga4_standard
    python scenario.py product_detail
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, List, Optional

import yaml
from selenium.common.exceptions import TimeoutException, WebDriverException

import config
from core import (
    CheckResult,
    Severity,
    _click,
    accept_consent,
    check_event,
    failed_check,
    get_datalayer_length,
    make_driver,
    print_report,
)
from spec import Expectation, Spec, load_spec

_SCENARIOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")

# A scenario refers to selectors by stable name; config holds the actual CSS.
# Keeping this mapping here means scenarios never embed raw selectors.
_SELECTOR_REFS = {
    "product_card":        "PRODUCT_CARD",
    "add_to_cart":         "ADD_TO_CART_BUTTON",
    "cart_remove":         "CART_REMOVE_BUTTON",
    "proceed_to_checkout": "PROCEED_TO_CHECKOUT_BUTTON",
    "search_input":        "SEARCH_INPUT",
    "search_submit":       "SEARCH_SUBMIT",
}


def _resolve_selector(ref: str) -> str:
    attr = _SELECTOR_REFS.get(ref)
    if attr is None:
        raise KeyError(f"unknown selector_ref '{ref}'")
    return getattr(config, attr)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _sev(spec: Spec, event: str) -> Severity:
    exp = spec.get(event)
    return exp.severity if exp else Severity.HIGH


def _dig(event: dict, path: str) -> Optional[Any]:
    """Resolve a dot-notation path within an event dict; None if absent."""
    value: Any = event
    for part in path.split("."):
        try:
            if isinstance(value, (list, tuple)):
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value[part]
            else:
                return None
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return value


def _check_patterns(result: CheckResult, exp: Expectation) -> List[CheckResult]:
    """Emit one extra check per param pattern rule (e.g. transaction_id format)."""
    extra: List[CheckResult] = []
    if result.event is None:
        return extra
    for param, rule in exp.params.items():
        if not rule.pattern:
            continue
        value = _dig(result.event, param)
        ok = value is not None and re.match(rule.pattern, str(value)) is not None
        extra.append(CheckResult(
            name=f"{exp.name}.{param} format",
            event=result.event,
            passed=bool(ok),
            detail=(f"{value!r} matches /{rule.pattern}/" if ok
                    else f"{value!r} does not match /{rule.pattern}/"),
            severity=exp.severity,
        ))
    return extra


def _scenario_path(name: str) -> str:
    path = os.path.join(_SCENARIOS_DIR, name)
    if not path.endswith((".yaml", ".yml")):
        path += ".yaml"
    return path


def load_scenario_steps(name: str) -> List[dict]:
    with open(_scenario_path(name)) as f:
        raw = yaml.safe_load(f) or {}
    # Accept either {name: {steps: [...]}} or a bare {steps: [...]}.
    body = raw.get(name, raw)
    return body.get("steps") or []


def run_scenario(name: str, spec: Spec) -> List[CheckResult]:
    steps = load_scenario_steps(name)
    driver = make_driver()
    results: List[CheckResult] = []
    after_index = 0
    skip_events: set[str] = set()
    try:
        for step in steps:
            if "goto" in step:
                path = step["goto"]
                url = (path if path.startswith("http")
                       else config.BASE_URL.rstrip("/") + "/" + path.lstrip("/"))
                driver.get(url)
                after_index = 0  # page-load events fire from the very start

            elif step.get("accept_consent"):
                accept_consent(driver)

            elif "click" in step:
                c = step["click"]
                ref = c["selector_ref"]
                # Capture index *before* the click so we catch the event it triggers.
                after_index = get_datalayer_length(driver)
                try:
                    _click(driver, _resolve_selector(ref))
                except (TimeoutException, WebDriverException) as exc:
                    for event in _as_list(c.get("on_fail")):
                        results.append(failed_check(
                            event,
                            f"Could not click '{ref}' "
                            f"(selector '{_resolve_selector(ref)}'): {exc}",
                            severity=_sev(spec, event),
                        ))
                        skip_events.add(event)

            elif "expect" in step:
                event = step["expect"]
                if event in skip_events:
                    continue
                exp = spec.get(event)
                if exp is None:
                    results.append(failed_check(
                        event, f"No expectation for '{event}' in the loaded spec"))
                    continue
                res = check_event(
                    driver, event, exp.required, after_index, severity=exp.severity)
                results.append(res)
                results.extend(_check_patterns(res, exp))
    finally:
        driver.quit()
    return results


def main() -> None:
    scenario = sys.argv[1] if len(sys.argv) > 1 else "product_detail"
    spec = load_spec("ga4_standard")
    results = run_scenario(scenario, spec)
    sys.exit(print_report([(scenario, results)]))


if __name__ == "__main__":
    main()
