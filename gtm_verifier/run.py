"""
GTM / GA4 dataLayer verifier

Usage:
    python run.py                                    # all journeys, default config
    python run.py page_load consent shop             # specific journeys, default config
    python run.py --config staging.yaml              # all journeys, custom config file
    python run.py --config staging.yaml shop cart    # specific journeys, custom config file
"""

import argparse
import sys
import traceback
from typing import Dict, List, Tuple

import config  # auto-loads config.yaml on import
from core import CheckResult, failed_check, print_report
import journeys

JOURNEY_MAP: Dict[str, callable] = {
    "page_load":      journeys.journey_page_load,
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
    exit_code = print_report(journey_results)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
