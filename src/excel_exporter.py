"""Export data quality findings to Excel."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


class ExcelExporter:
    """Write pipeline results to a multi-sheet Excel governance report."""

    def __init__(self, settings: dict[str, Any]) -> None:
        """Initialize output directory and filename prefix from settings.

        Args:
            settings: Pipeline settings dict (uses the ``output`` section).
        """
        output_cfg = settings.get("output", {})
        self.output_dir = Path(output_cfg.get("directory", "output"))
        self.filename_prefix = output_cfg.get("filename_prefix", "dq_governance_report")

    def export(
        self,
        row_issues: pd.DataFrame,
        rule_summary: pd.DataFrame,
        table_profiles: pd.DataFrame,
        llm_table_summaries: pd.DataFrame | None = None,
    ) -> Path:
        """Write all pipeline results to a timestamped Excel file.

        Sheets produced (in order): Overview, Rule_Summary, Table_Profiles,
        Row_Issues, and LLM_Table_Summaries.

        Args:
            row_issues: All row-level and table-level issues found.
            rule_summary: Issue counts grouped by rule and severity.
            table_profiles: Per-table profiling summary rows.
            llm_table_summaries: Optional LLM narrative per table.

        Returns:
            Path to the written ``.xlsx`` file.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = self.output_dir / f"{self.filename_prefix}_{timestamp}.xlsx"

        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            overview = self._build_overview(row_issues, rule_summary, table_profiles)
            overview.to_excel(writer, sheet_name="Overview", index=False)
            self._write_sheet(writer, "Rule_Summary", rule_summary)
            self._write_sheet(writer, "Table_Profiles", table_profiles)
            self._write_sheet(writer, "Row_Issues", row_issues)
            self._write_sheet(writer, "LLM_Table_Summaries", llm_table_summaries)

        return output_path

    def _write_sheet(self, writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame) -> None:
        """Write a DataFrame to a named sheet, or a placeholder if empty."""
        if df is None or df.empty:
            pd.DataFrame({"message": ["No issues found"]}).to_excel(
                writer, sheet_name=sheet_name, index=False
            )
        else:
            df.to_excel(writer, sheet_name=sheet_name, index=False)

    def _build_overview(
        self,
        row_issues: pd.DataFrame,
        rule_summary: pd.DataFrame,
        table_profiles: pd.DataFrame,
    ) -> pd.DataFrame:
        """Build the high-level metrics sheet for the Excel report.

        Args:
            row_issues: All issues found across profiling and business rules.
            rule_summary: Grouped issue counts (unused directly; kept for future use).
            table_profiles: Per-table profile rows used to count tables scanned.

        Returns:
            Two-column DataFrame with Metric and Value columns.
        """
        total_issues = len(row_issues) if not row_issues.empty else 0
        tables_scanned = (
            table_profiles["TABLE_NAME"].nunique() if not table_profiles.empty else 0
        )
        critical = (
            len(row_issues[row_issues["SEVERITY"] == "critical"])
            if not row_issues.empty and "SEVERITY" in row_issues.columns
            else 0
        )
        high = (
            len(row_issues[row_issues["SEVERITY"] == "high"])
            if not row_issues.empty and "SEVERITY" in row_issues.columns
            else 0
        )

        return pd.DataFrame(
            [
                {"Metric": "Total row-level issues", "Value": total_issues},
                {"Metric": "Tables scanned", "Value": tables_scanned},
                {"Metric": "Critical issues", "Value": critical},
                {"Metric": "High severity issues", "Value": high},
                {"Metric": "Report generated", "Value": datetime.now().isoformat()},
            ]
        )
