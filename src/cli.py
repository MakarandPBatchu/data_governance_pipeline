"""Command-line argument parsing for the data governance pipeline."""

from __future__ import annotations

import argparse
from dataclasses import dataclass


DEFAULT_CUSTOM_RULES_PATH = "config/custom_rules.csv"


@dataclass
class CliArgs:
    """Parsed command-line options for main.py."""

    skip_llm: bool
    log_level: str
    custom_rules: str | None


def build_parser() -> argparse.ArgumentParser:
    """Create the ArgumentParser with all supported CLI flags."""
    parser = argparse.ArgumentParser(
        description="Scan Snowflake tables for data quality and governance issues.",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip Llama 3.2 issue enrichment (SQL generation for --custom-rules still runs).",
    )
    parser.add_argument(
        "--custom-rules",
        nargs="?",
        const=DEFAULT_CUSTOM_RULES_PATH,
        default=None,
        metavar="PATH",
        help=(
            "Run only natural-language custom rules from a CSV template. "
            "Skips profiling and rules.yaml. If PATH is omitted, uses "
            f"{DEFAULT_CUSTOM_RULES_PATH}."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity level.",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> CliArgs:
    """Parse CLI arguments into a typed ``CliArgs`` object.

    Args:
        argv: Optional argument list (defaults to ``sys.argv`` when ``None``).

    Returns:
        Parsed ``CliArgs`` with ``skip_llm``, ``custom_rules``, and ``log_level`` fields.
    """
    args = build_parser().parse_args(argv)
    return CliArgs(
        skip_llm=args.skip_llm,
        log_level=args.log_level,
        custom_rules=args.custom_rules,
    )
