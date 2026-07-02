"""
GTM / GA4 website audit tool

Two modes:

  Foreign site (no config needed) — the six infrastructure audits run on any URL:
    python run.py --url https://prospect.com
    python run.py --url https://prospect.com analytics_audit consent_audit

  Client site (YAML config with journeys) — adds deep dataLayer verification:
    python run.py                                    # config.yaml: all audits + journeys
    python run.py --config client.yaml               # a specific client
    python run.py --config client.yaml shop cart     # specific journeys only
    python run.py --url https://staging.client.com --config client.yaml   # same journeys, other env

  python run.py --list                               # show available audits
  python run.py --export report.pptx                 # PowerPoint instead of terminal
"""

import argparse
import sys
import traceback
from typing import Callable, Dict, List, Tuple

import config  # auto-loads config.yaml on import when present
from core import CheckResult, failed_check, print_report
from export import export_to_powerpoint
import journeys

# Site-agnostic audits — run on any public URL, no selectors or journeys needed.
SITE_AUDITS: Dict[str, Tuple[Callable, str]] = {
    "analytics_audit":  (journeys.journey_analytics_audit,  "GA4/GTM presence, deployment method, sGTM signals"),
    "consent_audit":    (journeys.journey_consent_audit,    "CMP banner, Consent Mode v2, pre/post-consent firing"),
    "network_audit":    (journeys.journey_network_audit,    "GA4 collect traffic — cid/sid, consent state, event inventory"),
    "tag_inventory":    (journeys.journey_tag_inventory,    "All marketing/analytics tags and pixels on the page"),
    "seo":              (journeys.journey_seo,              "SEO & metadata checks"),
    "security_headers": (journeys.journey_security_headers, "HTTP security header checks"),
}


def _available() -> Dict[str, Callable]:
    """Site audits plus whatever journeys the loaded config defines."""
    audits: Dict[str, Callable] = {name: fn for name, (fn, _d) in SITE_AUDITS.items()}
    for jname, spec in config.JOURNEYS.items():
        if jname in audits:
            print(f"Warning: journey '{jname}' shadows a built-in audit — ignored.", file=sys.stderr)
            continue
        audits[jname] = lambda n=jname, s=spec: journeys.run_journey(n, s)
    return audits


def _print_list() -> None:
    print("Site-agnostic audits (any URL, no config needed):")
    for name, (_fn, desc) in SITE_AUDITS.items():
        print(f"  {name:<18} {desc}")
    if config.JOURNEYS:
        src = config.CONFIG_PATH or "config"
        print(f"\nJourneys defined in {src}:")
        for jname, spec in config.JOURNEYS.items():
            desc = (spec or {}).get("description", "")
            print(f"  {jname:<18} {desc}")
    else:
        print("\nNo journeys defined — add a 'journeys:' section to a config file "
              "for site-specific dataLayer verification.")


def _run(names: List[str], available: Dict[str, Callable]) -> List[Tuple[str, List[CheckResult]]]:
    results = []
    for name in names:
        if name not in available:
            print(
                f"Unknown audit '{name}'. Available: {', '.join(available)}",
                file=sys.stderr,
            )
            continue
        print(f"Running: {name} …")
        try:
            checks = available[name]()
        except Exception as exc:
            tb = traceback.format_exc()
            checks = [failed_check(name, f"Audit crashed unexpectedly: {exc}\n{tb}")]
        results.append((name, checks))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit a website's GTM / GA4 implementation via headless Chrome."
    )
    parser.add_argument(
        "--url", "-u",
        metavar="URL",
        help="Target site URL. Works with no config file at all (site-agnostic audits only).",
    )
    parser.add_argument(
        "--config", "-c",
        metavar="FILE",
        help="Path to a YAML config file (default: config.yaml in this directory).",
    )
    parser.add_argument(
        "--export", "-e",
        metavar="FILE.pptx",
        help="Export results to PowerPoint file instead of terminal output.",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List available audits and configured journeys, then exit.",
    )
    parser.add_argument(
        "audits",
        nargs="*",
        metavar="AUDIT",
        help="Audit/journey names to run. Leave empty to run everything applicable.",
    )
    args = parser.parse_args()

    if args.config:
        try:
            config.load(args.config)
            print(f"Config: {args.config}")
        except FileNotFoundError:
            print(f"Config file not found: {args.config}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            print(f"Failed to load config '{args.config}': {exc}", file=sys.stderr)
            sys.exit(1)

    if args.url:
        config.set_base_url(args.url)

    available = _available()

    if args.list:
        _print_list()
        sys.exit(0)

    if not config.BASE_URL:
        print(
            "No target site. Pass --url <site>, or provide a config file with site.base_url\n"
            "(copy config.example.yaml to config.yaml, or use --config <file>).",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.audits:
        requested = args.audits
    elif args.url and not args.config:
        # Foreign-site mode: an ad-hoc URL with no explicit config gets only the
        # site-agnostic audits — configured journeys belong to a different site.
        requested = list(SITE_AUDITS)
    else:
        requested = list(SITE_AUDITS) + list(config.JOURNEYS)

    print(f"Target: {config.BASE_URL}")
    journey_results = _run(requested, available)
    if not journey_results:
        print("No audits were run.", file=sys.stderr)
        sys.exit(1)

    if args.export:
        try:
            export_to_powerpoint(journey_results, config.BASE_URL, args.export)
            sys.exit(0)
        except Exception as exc:
            print(f"Failed to export to PowerPoint: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        exit_code = print_report(journey_results)
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
