"""Configuration loader.

Two ways to point the tool at a site:

  1. No config file at all — pass --url on the command line (or use the web
     app). Only the six infrastructure audits run; they need nothing but a URL.
  2. A YAML config file (config.yaml by default, or --config client.yaml) —
     adds expected tag IDs, a site-specific consent selector, and declarative
     `journeys:` definitions for deep dataLayer verification.

Every key is optional. Missing keys fall back to the defaults below, so a
minimal client config can be just:

    site:
      base_url: "https://client.com"
"""

import os
from typing import Optional

import yaml

_DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

# ── Defaults — overridden by load() / set_base_url() ──────────────────────────
CONFIG_PATH: Optional[str] = None    # path of the loaded file; None if running on defaults
BASE_URL: Optional[str] = None

GTM_ID: Optional[str] = None         # expected GTM container — verified against the live site when set
GA4_ID: Optional[str] = None         # expected GA4 measurement ID — verified when set

DEFAULT_TIMEOUT = 10                 # seconds — WebDriverWait for DOM elements
EVENT_POLL_TIMEOUT = 15              # seconds — dataLayer event polling

CONSENT_ACCEPT_BUTTON: Optional[str] = None  # site-specific CMP accept selector (common CMPs are auto-detected)

JOURNEYS: dict = {}                  # journey name -> spec; see journeys.py for the step schema


def _real_id(value: Optional[str]) -> Optional[str]:
    """Filter template placeholders (GTM-XXXXXXX / G-XXXXXXXXXX) left in a config."""
    if not value or "XXXX" in value.upper():
        return None
    return value


def load(path: str = _DEFAULT_CONFIG) -> None:
    """Load (or reload) configuration from a YAML file."""
    with open(path) as f:
        c = yaml.safe_load(f) or {}
    load_dict(c, path=path)


def load_dict(c: dict, path: Optional[str] = None) -> None:
    """Apply an already-parsed config mapping (e.g. YAML uploaded via the web
    app). Every key is optional; unknown keys are ignored so client configs
    can carry their own notes."""
    global CONFIG_PATH, BASE_URL, GTM_ID, GA4_ID
    global DEFAULT_TIMEOUT, EVENT_POLL_TIMEOUT, CONSENT_ACCEPT_BUTTON, JOURNEYS

    site = c.get("site") or {}
    tags = c.get("tags") or {}
    timeouts = c.get("timeouts") or {}
    selectors = c.get("selectors") or {}

    CONFIG_PATH = path
    base = site.get("base_url")
    BASE_URL = base.rstrip("/") if base else None
    GTM_ID = _real_id(tags.get("gtm_id"))
    GA4_ID = _real_id(tags.get("ga4_id"))
    DEFAULT_TIMEOUT = timeouts.get("default") or DEFAULT_TIMEOUT
    EVENT_POLL_TIMEOUT = timeouts.get("event_poll") or EVENT_POLL_TIMEOUT
    CONSENT_ACCEPT_BUTTON = (selectors.get("consent") or {}).get("accept_button")
    JOURNEYS = c.get("journeys") or {}


def set_base_url(url: str) -> None:
    """Point the audits at an arbitrary URL (--url mode / web app)."""
    global BASE_URL
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    BASE_URL = url.rstrip("/")


# Auto-load the default config when present; absence is fine (--url mode).
if os.path.exists(_DEFAULT_CONFIG):
    load()
