"""Command-line entry point for publication-owned orchestration."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import sys

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
    return 0
