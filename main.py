"""Entry point for the Snowflake data governance pipeline."""

from __future__ import annotations

import logging
import sys

from src.cli import parse_args
from src.logging_config import configure_logging
from src.pipeline import DataGovernancePipeline

# TO DO: 
# 1. TEST FOR MORE NUMBER OF ROWS PER TABLES
# 2. SEE WHAT MORE DYNAMIC RULES CAN BE ADDED
# 4. HOW TO MAKE BUSINESS RULES MORE DYNAMIC AND EASY TO ADD/REMOVE/EDIT
def main() -> int:
    """Run the data governance pipeline and print a summary.

    Returns:
        0 on success, 1 if the pipeline raised an unhandled exception.
    """
    args = parse_args()
    log_file = configure_logging(level=args.log_level)

    try:
        pipeline = DataGovernancePipeline()
        result = pipeline.run(skip_llm=args.skip_llm)
        print("\nPipeline complete.")
        print(f"  Tables scanned : {result['tables_scanned']}")
        print(f"  Total issues   : {result['total_issues']}")
        print(f"  Report         : {result['output_path']}")
        print(f"  Log file       : {log_file}")
        return 0
    except Exception as exc:
        logging.exception("Pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
