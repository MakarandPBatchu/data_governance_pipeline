"""Orchestrate Snowflake scanning, profiling, LLM analysis, and Excel export."""

from __future__ import annotations

import logging
from typing import Any
import time

import pandas as pd
from tqdm import tqdm

from src.config_loader import load_settings
from src.custom_rules import CustomRuleRunner
from src.excel_exporter import ExcelExporter
from src.llm_analyzer import LlamaAnalyzer
from src.profiler import TableProfiler
from src.rule_engine import RuleEngine
from src.snowflake_client import SnowflakeClient

logger = logging.getLogger(__name__)

_TABLE_LEVEL = "TABLE_LEVEL"
_CLEAN_TABLE_SUMMARY = (
    "No issues were identified in this table as per the given rules."
)


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
        self.custom_rules = CustomRuleRunner(
            self.client, self.llm, self.rule_engine, self.settings
        )
        self.exporter = ExcelExporter(self.settings)

    def run(
        self,
        skip_llm: bool = False,
        custom_rules_path: str | None = None,
    ) -> dict[str, Any]:
        """Execute the data governance scan and write the Excel report.

        When ``custom_rules_path`` is set, profiling and ``rules.yaml`` are skipped
        and only LLM-generated SQL from the custom-rules CSV is executed.

        When ``custom_rules_path`` is omitted, the regular flow runs:
        1. Connect to Snowflake and list all tables in the target schema.
        2. Profile each table (null rates, duplicate PKs).
        3. Run business rules from ``config/rules.yaml``.
        4. Optionally enrich issues with Llama 3.2 narrative analysis.
        5. Export results to ``output/dq_governance_report_<timestamp>.xlsx``.

        Args:
            skip_llm: When ``True``, skip table summaries and issue enrichment.
                Custom-rule SQL generation still uses Ollama when ``custom_rules_path`` is set.
            custom_rules_path: Optional CSV of natural-language rules. When set,
                YAML and dynamic profiling rules are not run.

        Returns:
            Dict with keys: output_path, tables_scanned, total_issues, rule_summary.

        Raises:
            ValueError: If SNOWFLAKE_DATABASE or SNOWFLAKE_SCHEMA is not configured.
            FileNotFoundError: If ``custom_rules_path`` does not exist.
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

        needs_llm = (not skip_llm) or bool(custom_rules_path)
        if needs_llm:
            logger.info("Verifying Ollama / Llama 3.2 connection...")
            self.llm.verify_connection()

        tables = self.client.list_tables(database, schema)
        logger.info("Found %d tables in schema", len(tables))

        profile_issues: list[pd.DataFrame] = []
        profile_rows: list[dict[str, Any]] = []
        llm_table_summaries: list[dict[str, str]] = []
        business_issues = pd.DataFrame()
        custom_issues = pd.DataFrame()
        tables_scanned = len(tables)

        if custom_rules_path:
            logger.info(
                "Custom rules mode: skipping profiling and rules.yaml; using %s",
                custom_rules_path,
            )
            custom_issues = self.custom_rules.run(
                custom_rules_path, database, schema, tables
            )
            tables_scanned = self.custom_rules.tables_targeted
        else:
            profile_issues, profile_rows, llm_table_summaries = self._run_profiling(
                database, schema, tables, skip_llm
            )
            rule_flags = self.settings.get("rule_flags", {})
            if rule_flags.get("run_business_rules", True):
                logger.info("Running business rules...")
                business_issues = self.rule_engine.run_all(database, schema)

        all_issues = pd.concat(
            [
                df
                for df in [
                    pd.concat(profile_issues, ignore_index=True) if profile_issues else pd.DataFrame(),
                    business_issues,
                    custom_issues,
                ]
                if not df.empty
            ],
            ignore_index=True,
        )

        row_issues = self._without_table_level(all_issues)
        table_level_issue_count = (
            len(all_issues) - len(row_issues) if not all_issues.empty else 0
        )

        if not skip_llm and not row_issues.empty:
            logger.info("Enriching issues with Llama 3.2 analysis...")
            row_issues = self.llm.enrich_issues(row_issues)

        if not skip_llm:
            summary_tables = (
                self.custom_rules.targeted_table_names if custom_rules_path else tables
            )
            llm_table_summaries = self._add_clean_table_summaries(
                llm_table_summaries, summary_tables, all_issues
            )

        rule_summary = self.rule_engine.summarize_by_rule(all_issues)
        table_profiles_df = pd.DataFrame(profile_rows)
        llm_summaries_df = pd.DataFrame(llm_table_summaries)

        output_path = self.exporter.export(
            row_issues=row_issues,
            rule_summary=rule_summary,
            table_profiles=table_profiles_df,
            llm_table_summaries=llm_summaries_df,
            table_level_issue_count=table_level_issue_count,
        )

        logger.info("Report written to %s", output_path)
        self.client.close()

        return {
            "output_path": str(output_path),
            "tables_scanned": tables_scanned,
            "total_row_issues": len(row_issues),
            "total_table_issues": table_level_issue_count,
            "rule_summary": rule_summary,
        }

    def _run_profiling(
        self,
        database: str,
        schema: str,
        tables: list[str],
        skip_llm: bool,
    ) -> tuple[list[pd.DataFrame], list[dict[str, Any]], list[dict[str, str]]]:
        """Profile tables for null rates and duplicate PKs; optionally summarize with the LLM."""
        profile_issues: list[pd.DataFrame] = []
        profile_rows: list[dict[str, Any]] = []
        llm_table_summaries: list[dict[str, str]] = []
        avg_time_per_table_summary: list[float] = []

        rule_flags = self.settings.get("rule_flags", {})
        if not rule_flags.get("run_generic_profiling", True):
            return profile_issues, profile_rows, llm_table_summaries

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

        return profile_issues, profile_rows, llm_table_summaries

    @staticmethod
    def _without_table_level(issues_df: pd.DataFrame) -> pd.DataFrame:
        """Drop table-level profile findings so Row_Issues stays row-specific."""
        if issues_df.empty or "ROW_IDENTIFIER" not in issues_df.columns:
            return issues_df
        mask = issues_df["ROW_IDENTIFIER"].astype(str).str.upper() != _TABLE_LEVEL
        return issues_df.loc[mask].copy()

    @staticmethod
    def _add_clean_table_summaries(
        llm_table_summaries: list[dict[str, str]],
        tables: list[str],
        all_issues: pd.DataFrame,
    ) -> list[dict[str, str]]:
        """Add a no-findings note for scanned tables that produced no issues."""
        existing = {str(row["TABLE_NAME"]).upper() for row in llm_table_summaries}
        tables_with_issues: set[str] = set()
        if not all_issues.empty and "TABLE_NAME" in all_issues.columns:
            tables_with_issues = {
                str(name).upper() for name in all_issues["TABLE_NAME"].dropna()
            }

        summaries = list(llm_table_summaries)
        for table in tables:
            if table.upper() in existing or table.upper() in tables_with_issues:
                continue
            summaries.append(
                {"TABLE_NAME": table, "LLM_SUMMARY": _CLEAN_TABLE_SUMMARY}
            )
        return summaries
