"""
GA4 remote-config extraction — PUBLIC stream, pure HTTP (no browser).

Google serves each GA4 property's admin configuration inside the public gtag
loader (`googletagmanager.com/gtag/js?id=G-XXXX`): the script embeds a
`var data = {...}` JSON blob whose `resource.tags` entries encode property
settings — enhanced measurement toggles, key (conversion) events, Google
Signals, PII redaction, cross-domain linking, referral exclusions, session
timeout, EU DMA delegation. This audit fetches that script and decodes the
blob, revealing admin-level settings with no account access — the same
outside-in trick ga4spy.com uses.

Given a URL instead of a measurement ID, IDs are discovered from the page
HTML; on GTM-only sites the public gtm.js container script is fetched and
scanned for the G- IDs it configures.

Caveat: a bogus/retired ID does NOT 404 — Google returns a generic fallback
script (HTTP 200). A config only counts as resolved when a tag carries the
requested ID as a literal (`vtp_trackingId` / `vtp_instanceDestinationId`);
the fallback carries a macro reference there instead.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import network
from core import CheckResult, Severity, skip_check
from httpfetch import fetch

GTAG_URL = "https://www.googletagmanager.com/gtag/js?id={mid}"
GTM_URL = "https://www.googletagmanager.com/gtm.js?id={cid}"

# Same ID shapes as analytics.py — kept in sync.
_RE_GA4_ID = re.compile(r'\bG-[A-Z0-9]{6,}\b')
_RE_GTM_ID = re.compile(r'\bGTM-[A-Z0-9]{4,}\b')

_MAX_PROPERTIES = 3   # audit at most this many discovered IDs per page
_MAX_CONTAINERS = 3   # scan at most this many gtm.js containers for G- IDs

# Enhanced-measurement composite tags: present in the blob = toggle ON in admin.
_EM_TAGS = {
    "__ccd_em_page_view":      "page_view",
    "__ccd_em_scroll":         "scroll",
    "__ccd_em_outbound_click": "outbound_click",
    "__ccd_em_site_search":    "site_search",
    "__ccd_em_video":          "video",
    "__ccd_em_download":       "download",
    "__ccd_em_form":           "form",
}

# Setting tags this parser decodes (beyond _EM_TAGS).
_MODELLED_TAGS = {
    "__ccd_conversion_marking", "__ogt_google_signals", "__ccd_auto_redact",
    "__ogt_cross_domain", "__ogt_referral_exclusion", "__ogt_session_timeout",
    "__ogt_dma", "__ccd_ga_regscope",
}

# Plumbing present in every config (incl. the fallback) — carries no setting.
_PLUMBING_TAGS = {
    "__ccd_ga_first", "__ccd_ga_last", "__gct", "__dest_ga",
    "__set_product_settings", "__ogt_1p_data_v2", "__ccd_add_1p_data",
    "__ccd_add_ecs", "__ccd_ga_ads_link",
}


@dataclass
class RemoteConfig:
    """Decoded admin settings for one GA4 measurement ID."""
    measurement_id: str
    resolved: bool = False                # literal-ID discriminator (see module docstring)
    enhanced_measurement: Dict[str, bool] = field(default_factory=dict)
    key_events: List[str] = field(default_factory=list)
    google_signals: Optional[str] = None  # "ENABLED" / "DISABLED"
    redact_email: Optional[bool] = None
    redact_query_params: List[str] = field(default_factory=list)
    cross_domain_patterns: List[str] = field(default_factory=list)   # regexes as configured
    referral_exclusions: List[str] = field(default_factory=list)     # regexes as configured
    session_timeout_minutes: Optional[int] = None
    dma_delegation: Optional[str] = None  # EU Digital Markets Act consent delegation
    dma_default: Optional[str] = None
    transport_url: Optional[str] = None   # sGTM server container, when routed server-side
    regional_settings: list = field(default_factory=list)
    unknown_tags: List[str] = field(default_factory=list)  # __ogt_/__ccd_ we don't decode
    raw_tag_functions: List[str] = field(default_factory=list)
    fetch_status: int = 0
    fetch_bytes: int = 0
    error: Optional[str] = None


# ── Blob extraction & GTM-serialization helpers ───────────────────────────────

def _extract_data_blob(js_text: str) -> Optional[dict]:
    """Pull the `var data = {...}` JSON object out of a gtag.js script using a
    string-aware brace matcher (the blob contains `}` inside string values)."""
    start = js_text.find("var data = {")
    if start < 0:
        return None
    i = js_text.find("{", start)
    depth, in_str, esc = 0, False, False
    for j in range(i, len(js_text)):
        c = js_text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(js_text[i:j + 1])
                    except ValueError:
                        return None
    return None


def _from_gtm_value(value):
    """Normalize GTM's serialized values: ["list", a, b] -> [a, b],
    ["map", k1, v1, ...] -> {k1: v1, ...}; scalars pass through."""
    if isinstance(value, list) and value:
        kind = value[0]
        if kind == "list":
            return [_from_gtm_value(v) for v in value[1:]]
        if kind == "map":
            items = value[1:]
            return {items[k]: _from_gtm_value(items[k + 1]) for k in range(0, len(items) - 1, 2)}
    return value


def _unescape_domain_regex(pattern: str) -> str:
    """Admin-configured domains arrive regex-escaped ("app\\.asana\\.com").
    De-escape for display; leave genuine regex syntax (^, |) intact."""
    return str(pattern).replace("\\.", ".")


def _parse_key_events(tag: dict) -> List[str]:
    """Decode __ccd_conversion_marking: each rule is a ["map", "matchingRules",
    "<json>"] whose inner JSON matches an eventName against a stringValue.
    Unknown rule shapes degrade to collecting every stringValue found."""
    events: List[str] = []
    for rule in _from_gtm_value(tag.get("vtp_conversionRules")) or []:
        raw = rule.get("matchingRules") if isinstance(rule, dict) else rule
        if not isinstance(raw, str):
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        args = parsed.get("args") or []
        literals = [a["stringValue"] for a in args if isinstance(a, dict) and "stringValue" in a]
        names_ok = any(
            "eventName" in ((a.get("contextValue") or {}).get("keyParts") or [])
            for a in args if isinstance(a, dict)
        )
        if literals and (names_ok or len(literals) == 1):
            events.extend(literals)
    return sorted(set(events))


# ── Fetch + decode one property ───────────────────────────────────────────────

def fetch_remote_config(measurement_id: str) -> RemoteConfig:
    """Fetch and decode the public gtag config for one measurement ID.
    Never raises — transport/parse failures come back in `.error`."""
    rc = RemoteConfig(measurement_id=measurement_id)
    resp = fetch(GTAG_URL.format(mid=measurement_id))
    rc.fetch_status, rc.fetch_bytes = resp.status, len(resp.text)
    if not resp.ok:
        rc.error = resp.error or f"HTTP {resp.status}"
        return rc

    data = _extract_data_blob(resp.text)
    if data is None:
        rc.error = "config blob not found in gtag.js (format may have changed)"
        return rc
    tags = [t for t in (data.get("resource") or {}).get("tags", []) if isinstance(t, dict)]
    rc.raw_tag_functions = [t.get("function", "?") for t in tags]

    for t in tags:
        if measurement_id in (t.get("vtp_trackingId"), t.get("vtp_instanceDestinationId")):
            rc.resolved = True
        for key in ("vtp_serverContainerUrl", "vtp_transportUrl"):
            if isinstance(t.get(key), str):
                rc.transport_url = t[key]
    if not rc.resolved:
        return rc

    funcs = set(rc.raw_tag_functions)
    rc.enhanced_measurement = {name: fn in funcs for fn, name in _EM_TAGS.items()}
    rc.unknown_tags = sorted(
        f for f in funcs
        if f.startswith(("__ogt_", "__ccd_"))
        and f not in _EM_TAGS and f not in _MODELLED_TAGS and f not in _PLUMBING_TAGS
    )

    for t in tags:
        fn = t.get("function")
        if fn == "__ccd_conversion_marking":
            rc.key_events = sorted(set(rc.key_events + _parse_key_events(t)))
        elif fn == "__ogt_google_signals":
            rc.google_signals = t.get("vtp_googleSignals")
        elif fn == "__ccd_auto_redact":
            rc.redact_email = bool(t.get("vtp_redactEmail"))
            raw = t.get("vtp_redactQueryParams") or ""
            rc.redact_query_params = [p for p in str(raw).split(",") if p]
        elif fn == "__ogt_cross_domain":
            rc.cross_domain_patterns = [
                _unescape_domain_regex(p) for p in _from_gtm_value(t.get("vtp_rules")) or []
            ]
        elif fn == "__ogt_referral_exclusion":
            rc.referral_exclusions = [
                _unescape_domain_regex(p)
                for p in _from_gtm_value(t.get("vtp_includeConditions")) or []
            ]
        elif fn == "__ogt_session_timeout":
            try:
                rc.session_timeout_minutes = (
                    int(t.get("vtp_sessionHours") or 0) * 60 + int(t.get("vtp_sessionMinutes") or 0)
                )
            except (TypeError, ValueError):
                pass
        elif fn == "__ogt_dma":
            rc.dma_delegation = t.get("vtp_delegationMode")
            rc.dma_default = t.get("vtp_dmaDefault")
        elif fn == "__ccd_ga_regscope":
            rc.regional_settings = _from_gtm_value(t.get("vtp_settingsTable")) or []
    return rc


# ── Measurement-ID discovery from a URL ───────────────────────────────────────

def discover_measurement_ids(url: str) -> Tuple[List[str], List[str], str]:
    """Return (ga4_ids, gtm_ids, how) discovered from a page without a browser.
    GTM-only sites fall back to scanning the public gtm.js container script,
    which embeds the G- IDs its GA4 tags are configured with."""
    from browser_fetch import resilient_fetch  # deferred: pulls in playwright

    page = resilient_fetch(url)
    if not page.ok:
        return [], [], f"page fetch failed: {page.error or f'HTTP {page.status}'}"
    ga4_ids = sorted(set(_RE_GA4_ID.findall(page.text)))
    gtm_ids = sorted(set(_RE_GTM_ID.findall(page.text)))
    if ga4_ids:
        return ga4_ids, gtm_ids, "page HTML"
    if gtm_ids:
        found: List[str] = []
        for cid in gtm_ids[:_MAX_CONTAINERS]:
            container = fetch(GTM_URL.format(cid=cid))
            if container.ok:
                found.extend(_RE_GA4_ID.findall(container.text))
        if found:
            return sorted(set(found)), gtm_ids, f"gtm.js container(s): {', '.join(gtm_ids[:_MAX_CONTAINERS])}"
    return [], gtm_ids, "page HTML"


# ── Audit entry point ─────────────────────────────────────────────────────────

def _info(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, event=None, passed=True, detail=detail, severity=Severity.INFO)


def _onoff(flag: bool) -> str:
    return "on" if flag else "off"


def _property_checks(rc: RemoteConfig, prefix: str = "") -> List[CheckResult]:
    """INFO observations for one resolved property config."""
    em = rc.enhanced_measurement
    results = [
        _info(f"{prefix}Enhanced measurement",
              ", ".join(f"{name} {_onoff(on)}" for name, on in em.items())
              if em else "(not present in config)"),
        _info(f"{prefix}Key events (conversions)",
              f"{len(rc.key_events)} configured: {', '.join(rc.key_events)}"
              if rc.key_events else "None configured beyond the default purchase marking"),
        _info(f"{prefix}Google Signals", rc.google_signals or "not configured"),
        _info(f"{prefix}PII redaction",
              f"email redaction {_onoff(bool(rc.redact_email))}"
              + (f"; query params: {', '.join(rc.redact_query_params)}"
                 if rc.redact_query_params else "; no query-param redaction")),
        _info(f"{prefix}Cross-domain linking",
              f"domains: {', '.join(rc.cross_domain_patterns)}"
              if rc.cross_domain_patterns else "not configured"),
        _info(f"{prefix}Server-side transport",
              rc.transport_url or "default Google endpoints (no server container URL)"),
    ]
    extras = []
    if rc.referral_exclusions:
        extras.append(f"referral exclusions: {', '.join(rc.referral_exclusions)}")
    if rc.session_timeout_minutes is not None:
        extras.append(f"session timeout: {rc.session_timeout_minutes} min")
    if rc.dma_delegation:
        extras.append(f"DMA consent delegation: {rc.dma_delegation}"
                      + (f" (default {rc.dma_default})" if rc.dma_default else ""))
    if rc.regional_settings:
        extras.append(f"regional controls: {len(rc.regional_settings)} rule(s)")
    if extras:
        results.append(_info(f"{prefix}Other property settings", "; ".join(extras)))
    if rc.unknown_tags:
        results.append(_info(
            f"{prefix}Undecoded config tags",
            "Present but not decoded by this audit: " + ", ".join(rc.unknown_tags),
        ))
    return results


def _expectation_checks(rc: RemoteConfig, expected: dict) -> List[CheckResult]:
    """PASS/FAIL comparisons against client-YAML declared admin settings."""
    results: List[CheckResult] = []

    want_events = expected.get("key_events") or []
    if want_events:
        missing = [e for e in want_events if e not in rc.key_events]
        results.append(CheckResult(
            name="Expected key events configured", event=None, passed=not missing,
            detail=(f"All configured: {', '.join(want_events)}" if not missing else
                    f"Missing from property config: {', '.join(missing)} "
                    f"(configured: {', '.join(rc.key_events) or 'none'})"),
            severity=Severity.HIGH,
        ))

    want_domains = expected.get("cross_domains") or []
    if want_domains:
        missing = [d for d in want_domains
                   if not any(re.search(p, d) for p in _safe_patterns(rc.cross_domain_patterns))]
        results.append(CheckResult(
            name="Expected cross-domain domains", event=None, passed=not missing,
            detail=(f"All covered: {', '.join(want_domains)}" if not missing else
                    f"Not covered by configured patterns: {', '.join(missing)} "
                    f"(configured: {', '.join(rc.cross_domain_patterns) or 'none'})"),
            severity=Severity.MEDIUM,
        ))

    want_em = expected.get("enhanced_measurement") or {}
    if want_em:
        wrong = [f"{k} is {_onoff(bool(rc.enhanced_measurement.get(k)))} (expected {_onoff(bool(v))})"
                 for k, v in want_em.items() if bool(rc.enhanced_measurement.get(k)) != bool(v)]
        results.append(CheckResult(
            name="Expected enhanced measurement", event=None, passed=not wrong,
            detail="Matches expectations" if not wrong else "; ".join(wrong),
            severity=Severity.MEDIUM,
        ))

    want_signals = expected.get("google_signals")
    if want_signals:
        got = rc.google_signals or "not configured"
        ok = got.upper() == str(want_signals).upper()
        results.append(CheckResult(
            name="Expected Google Signals state", event=None, passed=ok,
            detail=f"Google Signals is {got}" + ("" if ok else f" (expected {want_signals})"),
            severity=Severity.MEDIUM,
        ))
    return results


def _safe_patterns(patterns: List[str]) -> List[str]:
    """Drop admin-side patterns that don't compile as Python regexes."""
    ok = []
    for p in patterns:
        try:
            re.compile(p)
            ok.append(p)
        except re.error:
            pass
    return ok


def _cross_checks(rc: RemoteConfig, url: str) -> List[CheckResult]:
    """Declared config vs traffic observed by the network audit (same run)."""
    last = network.LAST_RUN
    if not last or last.get("url") != url:
        return [_info("Config vs observed traffic",
                      "network audit did not run on this URL in this session — cross-check skipped")]
    results: List[CheckResult] = []

    observed = set(last.get("event_names") or [])
    if rc.key_events:
        unseen = [e for e in rc.key_events if e not in observed]
        seen = [e for e in rc.key_events if e in observed]
        parts = []
        if seen:
            parts.append(f"observed on this page load: {', '.join(seen)}")
        if unseen:
            parts.append(f"not observed (may fire deeper in the funnel): {', '.join(unseen)}")
        results.append(_info("Key events vs observed traffic", "; ".join(parts)))

    host = re.sub(r"^https?://", "", url).split("/")[0]
    if rc.cross_domain_patterns:
        covered = any(re.search(p, host) for p in _safe_patterns(rc.cross_domain_patterns))
        results.append(_info(
            "Cross-domain declared vs site",
            f"audited host {host} {'is' if covered else 'is NOT'} covered by the "
            f"configured linker domains: {', '.join(rc.cross_domain_patterns)}",
        ))
    return results


def run_remote_config_audit(
    url: Optional[str] = None,
    measurement_id: Optional[str] = None,
    expected: Optional[dict] = None,
) -> List[CheckResult]:
    """Extract and report the GA4 property config for `measurement_id`, or for
    every ID discovered on `url` (capped at _MAX_PROPERTIES). An explicit ID is
    authoritative: failing to resolve it is a FAIL; a discovered ID that does
    not resolve is only a SKIP (regexes over HTML can catch stale IDs)."""
    results: List[CheckResult] = []
    ids: List[str] = []
    explicit = bool(measurement_id)

    if measurement_id:
        ids = [measurement_id]
        results.append(_info("Measurement ID discovery", f"Using supplied ID {measurement_id}"))
    elif url:
        ga4_ids, gtm_ids, how = discover_measurement_ids(url)
        if not ga4_ids:
            reason = (
                f"No GA4 measurement ID resolvable ({how}). "
                + (f"GTM container(s) present: {', '.join(gtm_ids)}. " if gtm_ids else "")
                + "Supply one explicitly with --measurement-id."
            )
            return [skip_check("GA4 remote config", reason, Severity.HIGH)]
        ids = ga4_ids[:_MAX_PROPERTIES]
        note = f"Found via {how}: {', '.join(ga4_ids)}"
        if len(ga4_ids) > _MAX_PROPERTIES:
            note += f" — auditing the first {_MAX_PROPERTIES}"
        results.append(_info("Measurement ID discovery", note))
    else:
        return [skip_check(
            "GA4 remote config",
            "Not assessed — no URL and no measurement ID supplied.",
            Severity.HIGH,
        )]

    configs = [fetch_remote_config(mid) for mid in ids]

    if all(c.error and not c.fetch_status for c in configs):
        return results + [skip_check(
            "GA4 remote config",
            f"Not assessed — googletagmanager.com unreachable: {configs[0].error}",
            Severity.HIGH,
        )]

    primary: Optional[RemoteConfig] = next((c for c in configs if c.resolved), None)
    multi = len(configs) > 1

    for rc in configs:
        prefix = f"{rc.measurement_id} · " if multi else ""
        results.append(_info(
            f"{prefix}Fetch",
            f"{GTAG_URL.format(mid=rc.measurement_id)} · HTTP {rc.fetch_status} · "
            f"{rc.fetch_bytes} bytes · {len(rc.raw_tag_functions)} config tags",
        ))
        if rc.resolved:
            results.append(CheckResult(
                name=f"{prefix}Property config resolved", event=None, passed=True,
                detail=f"{rc.measurement_id} resolves to a live property configuration",
                severity=Severity.HIGH,
            ))
            results.extend(_property_checks(rc, prefix))
        elif explicit:
            results.append(CheckResult(
                name=f"{prefix}Property config resolved", event=None, passed=False,
                detail=(
                    f"{rc.measurement_id} does not resolve to a property — Google served "
                    f"the generic fallback script ({rc.error or 'no literal ID in config'}). "
                    "The ID may be mistyped or the property deleted."
                ),
                severity=Severity.HIGH,
            ))
        else:
            results.append(skip_check(
                f"{prefix}Property config resolved",
                f"Discovered ID {rc.measurement_id} did not resolve "
                f"({rc.error or 'generic fallback script served'}) — possibly a stale "
                "ID left in the page source.",
                Severity.HIGH,
            ))

    if primary:
        if expected:
            results.extend(_expectation_checks(primary, expected))
        if url:
            results.extend(_cross_checks(primary, url))
    return results
