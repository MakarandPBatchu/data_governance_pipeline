"""Command-line argument parsing for the data governance pipeline."""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass
class CliArgs:
    """Parsed command-line options for main.py."""

    skip_llm: bool
    log_level: str


def build_parser() -> argparse.ArgumentParser:
    """Create the ArgumentParser with all supported CLI flags."""
    parser = argparse.ArgumentParser(
        description="Scan Snowflake tables for data quality and governance issues.",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip Llama 3.2 analysis (useful for testing Snowflake connectivity only).",
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
        Parsed ``CliArgs`` with ``skip_llm`` and ``log_level`` fields.
    """
    args = build_parser().parse_args(argv)
    return CliArgs(skip_llm=args.skip_llm, log_level=args.log_level)
