"""Orchestrate Snowflake scanning, profiling, LLM analysis, and Excel export."""

from __future__ import annotations

import logging
from typing import Any
import time

import pandas as pd
from tqdm import tqdm

from src.config_loader import load_settings
from src.excel_exporter import ExcelExporter
from src.llm_analyzer import LlamaAnalyzer
from src.profiler import TableProfiler
from src.rule_engine import RuleEngine
from src.snowflake_client import SnowflakeClient

logger = logging.getLogger(__name__)


class DataGovernancePipeline:
    """End-to-end pipeline: scan Snowflake, detect issues, enrich with LLM, export Excel."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        """Wire up all pipeline components.

        Args:
            settings: Optional pre-loaded settings dict; loads from disk when ``None``.
        """
        self.settings = settings or load_settings()
        self.client = SnowflakeClient(self.settings)
        self.profiler = TableProfiler(self.client, self.settings)
        self.rule_engine = RuleEngine(self.client, self.settings)
        self.llm = LlamaAnalyzer(self.settings)
        self.exporter = ExcelExporter(self.settings)

    def run(self, skip_llm: bool = False) -> dict[str, Any]:
        """Execute the full data governance scan and write the Excel report.

        Steps:
        1. Connect to Snowflake and list all tables in the target schema.
        2. Profile each table (null rates, duplicate PKs).
        3. Run business rules from ``config/rules.yaml``.
        4. Optionally enrich issues with Llama 3.2 narrative analysis.
        5. Export results to ``output/dq_governance_report_<timestamp>.xlsx``.

        Args:
            skip_llm: When ``True``, skip Ollama verification and all LLM enrichment.

        Returns:
            Dict with keys: output_path, tables_scanned, total_issues, rule_summary.

        Raises:
            ValueError: If SNOWFLAKE_DATABASE or SNOWFLAKE_SCHEMA is not configured.
        """
        sf = self.settings["snowflake"]
        database = sf.get("database")
        schema = sf.get("schema")

        if not database or not schema:
            raise ValueError(
                "SNOWFLAKE_DATABASE and SNOWFLAKE_SCHEMA must be set in .env before running."
            )

        logger.info("Connecting to Snowflake account=%s database=%s schema=%s", sf["account"], database, schema)
        self.client.connect()

        if not skip_llm:
            logger.info("Verifying Ollama / Llama 3.2 connection...")
            self.llm.verify_connection()

        tables = self.client.list_tables(database, schema)
        logger.info("Found %d tables to scan", len(tables))

        profile_issues: list[pd.DataFrame] = []
        profile_rows: list[dict[str, Any]] = []
        llm_table_summaries: list[dict[str, str]] = []
        avg_time_per_table_summary: list[float] = []

        rule_flags = self.settings.get("rule_flags", {})
        if rule_flags.get("run_generic_profiling", True):
            for table, pk_cols, row_count in tqdm(
                self.client.iter_table_batches(database, schema, tables),
                total=len(tables),
                desc="Profiling tables",
            ):
                profile = self.profiler.profile_table(database, schema, table, pk_cols, row_count)
                issue_df = self.profiler.profiles_to_issue_rows(profile)
                if not issue_df.empty:
                    issue_df["DATABASE"] = database
                    issue_df["SCHEMA"] = schema
                    profile_issues.append(issue_df)

                col_issues = [f"{c.column}: {c.issues[0]}" for c in profile.columns if c.issues]
                profile_rows.append(
                    {
                        "TABLE_NAME": table,
                        "ROW_COUNT": row_count,
                        "PRIMARY_KEYS": ", ".join(pk_cols),
                        "COLUMN_ISSUE_COUNT": len(col_issues),
                        "TABLE_ISSUE_COUNT": len(profile.issues),
                        "ISSUES": "; ".join(profile.issues + col_issues) or "None",
                    }
                )

                if not skip_llm and (col_issues or profile.issues):
                    start_time = time.time()

                    logger.info(f"Generating table summary for table - {table}")
                    summary = self.llm.analyze_table_profile(
                        table_name=table,
                        row_count=row_count,
                        column_issues=col_issues,
                        table_issues=profile.issues,
                    )

                    end_time = time.time()
                    avg_time_per_table_summary.append(end_time - start_time)
                    llm_table_summaries.append({"TABLE_NAME": table, "LLM_SUMMARY": summary})

        if avg_time_per_table_summary:
            logger.info(
                "Average time taken by LLM per table summary: %.2f seconds",
                sum(avg_time_per_table_summary) / len(avg_time_per_table_summary),
            )
        business_issues = pd.DataFrame()
        if rule_flags.get("run_business_rules", True):
            logger.info("Running business rules...")
            business_issues = self.rule_engine.run_all(database, schema)

        all_issues = pd.concat(
            [df for df in [pd.concat(profile_issues, ignore_index=True) if profile_issues else pd.DataFrame(), business_issues] if not df.empty],
            ignore_index=True,
        )

        if not skip_llm and not all_issues.empty:
            logger.info("Enriching issues with Llama 3.2 analysis...")
            all_issues = self.llm.enrich_issues(all_issues)

        rule_summary = self.rule_engine.summarize_by_rule(all_issues)
        table_profiles_df = pd.DataFrame(profile_rows)
        llm_summaries_df = pd.DataFrame(llm_table_summaries)

        output_path = self.exporter.export(
            row_issues=all_issues,
            rule_summary=rule_summary,
            table_profiles=table_profiles_df,
            llm_table_summaries=llm_summaries_df,
        )

        logger.info("Report written to %s", output_path)
        self.client.close()

        return {
            "output_path": str(output_path),
            "tables_scanned": len(tables),
            "total_issues": len(all_issues),
            "rule_summary": rule_summary,
        }
