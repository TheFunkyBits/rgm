"""Command-line entry point for publication-owned orchestration."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import sys

from . import catalog_lifecycle
from . import catalog_promotion


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.rgm_publication_tools",
        description="RGM publication orchestration tools",
    )
    commands = parser.add_subparsers(dest="command", required=True, title="commands")
    promote = commands.add_parser(
        "promote-external-catalog",
        help="prepare a reviewed catalog release worktree diff",
    )
    catalog_promotion.add_arguments(promote)
    verify_lifecycle = commands.add_parser(
        "verify-catalog-lifecycle",
        help="verify the current catalog lifecycle inventory without mutation",
    )
    catalog_lifecycle.add_verification_arguments(verify_lifecycle)
    plan_deprecation = commands.add_parser(
        "plan-catalog-deprecation",
        help="validate a local catalog deprecation event without mutation",
    )
    catalog_lifecycle.add_deprecation_arguments(plan_deprecation)
    prepare_deprecation = commands.add_parser(
        "prepare-catalog-deprecation",
        help="atomically prepare a local catalog deprecation event",
    )
    catalog_lifecycle.add_deprecation_arguments(prepare_deprecation)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(sys.argv[1:] if arguments is None else arguments)
    if parsed.command == "promote-external-catalog":
        try:
            result = catalog_promotion.promote_external_catalog(
                catalog_promotion.request_from_arguments(parsed)
            )
        except (OSError, RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    if parsed.command == "verify-catalog-lifecycle":
        try:
            result = catalog_lifecycle.verify_catalog_lifecycle(
                catalog_lifecycle.verification_request_from_arguments(parsed)
            )
        except (OSError, RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    if parsed.command == "plan-catalog-deprecation":
        try:
            result = catalog_lifecycle.plan_catalog_deprecation(
                catalog_lifecycle.deprecation_request_from_arguments(parsed)
            )
        except (OSError, RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    if parsed.command == "prepare-catalog-deprecation":
        try:
            result = catalog_lifecycle.prepare_catalog_deprecation(
                catalog_lifecycle.deprecation_request_from_arguments(parsed)
            )
        except (OSError, RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0
