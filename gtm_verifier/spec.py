"""
Expectations spec — the WHAT of an audit.

A spec maps a GA4 event name to the params it must carry and how serious a
failure is. It is deliberately decoupled from the HOW (scenarios drive the
browser; see scenario.py). The same Spec shape can come from two providers:

  - a bundled YAML standard (Stream 2: URL-only, best-practice audit)
  - generation from a client's GTM container (Stream 1: not yet built)

The runtime engine in core.py consumes a Spec without knowing its source.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

from core import Severity

_SPECS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "specs")


@dataclass
class ParamRule:
    """An extra constraint on a single param, e.g. a regex the value must match."""
    pattern: Optional[str] = None


@dataclass
class Expectation:
    name: str
    required: List[str] = field(default_factory=list)
    severity: Severity = Severity.HIGH
    params: Dict[str, ParamRule] = field(default_factory=dict)


@dataclass
class Spec:
    events: Dict[str, Expectation]

    def get(self, event_name: str) -> Optional[Expectation]:
        return self.events.get(event_name)


def load_spec(path: str) -> Spec:
    """Load a spec by absolute path, or by bare name resolved against specs/."""
    if not os.path.isabs(path):
        path = os.path.join(_SPECS_DIR, path)
        if not path.endswith((".yaml", ".yml")):
            path += ".yaml"

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    events: Dict[str, Expectation] = {}
    for name, body in (raw.get("events") or {}).items():
        body = body or {}
        params = {
            param: ParamRule(pattern=(rule or {}).get("pattern"))
            for param, rule in (body.get("params") or {}).items()
        }
        events[name] = Expectation(
            name=name,
            required=body.get("required") or [],
            severity=Severity(body.get("severity", "HIGH")),
            params=params,
        )
    return Spec(events=events)
