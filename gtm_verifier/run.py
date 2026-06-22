"""
GTM / GA4 dataLayer verifier

Usage:
    python run.py                                    # all journeys, terminal output
    python run.py page_load consent shop             # specific journeys
    python run.py --export report.pptx               # export to PowerPoint
    python run.py --config staging.yaml --export report.pptx
"""

import argparse
import sys
import traceback
from typing import Dict, List, Tuple

import config  # auto-loads config.yaml on import
from core import CheckResult, failed_check, print_report
from export import export_to_powerpoint
import journeys

JOURNEY_MAP: Dict[str, callable] = {
    "analytics_audit": journeys.journey_analytics_audit,
    "consent_audit":   journeys.journey_consent_audit,
    "network_audit":   journeys.journey_network_audit,
    "tag_inventory":   journeys.journey_tag_inventory,
    "seo":             journeys.journey_seo,
    "security_headers": journeys.journey_security_headers,
    "page_load":       journeys.journey_page_load,
    "consent":        journeys.journey_consent,
    "shop":           journeys.journey_shop,
    "product_detail": journeys.journey_product_detail,
    "cart":           journeys.journey_cart,
    "checkout":       journeys.journey_checkout,
    "purchase":       journeys.journey_purchase,
    "subscribe":      journeys.journey_subscribe,
    "contact":        journeys.journey_contact,
    "search":         journeys.journey_search,
}


def _run(names: List[str]) -> List[Tuple[str, List[CheckResult]]]:
    results = []
    for name in names:
        if name not in JOURNEY_MAP:
            print(
                f"Unknown journey '{name}'. "
                f"Available: {', '.join(JOURNEY_MAP)}",
                file=sys.stderr,
            )
            continue
        print(f"Running: {name} …")
        try:
            checks = JOURNEY_MAP[name]()
        except Exception as exc:
            tb = traceback.format_exc()
            checks = [failed_check(name, f"Journey crashed unexpectedly: {exc}\n{tb}")]
        results.append((name, checks))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify GTM / GA4 dataLayer events via headless Chrome."
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
        "journeys",
        nargs="*",
        metavar="JOURNEY",
        help=f"Journey names to run. Leave empty to run all. "
             f"Available: {', '.join(JOURNEY_MAP)}",
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

    requested = args.journeys or list(JOURNEY_MAP)
    journey_results = _run(requested)
    if not journey_results:
        print("No journeys were run.", file=sys.stderr)
        sys.exit(1)

    if args.export:
        # Export to PowerPoint
        try:
            export_to_powerpoint(journey_results, config.BASE_URL, args.export)
            sys.exit(0)
        except Exception as exc:
            print(f"Failed to export to PowerPoint: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        # Terminal output
        exit_code = print_report(journey_results)
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
