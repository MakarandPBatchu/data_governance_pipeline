"""Export data quality findings to Excel."""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet

_WRAP_COLUMNS = frozenset(
    {
        "ISSUE_DETAIL",
        "LLM_SUMMARY",
        "ISSUES",
        "RULE_NAME",
        "message",
    }
)
_MIN_COL_WIDTH = 12
_MAX_COL_WIDTH = 48
_TABLE_STYLE = "TableStyleMedium2"


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
        Row_Issues, and LLM_Table_Summaries. Each sheet is formatted as an
        Excel Table with a frozen header row and autofilter.

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
            self._apply_table_formatting(writer)

        return output_path

    def _write_sheet(self, writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame) -> None:
        """Write a DataFrame to a named sheet, or a placeholder if empty."""
        if df is None or df.empty:
            pd.DataFrame({"message": ["No issues found"]}).to_excel(
                writer, sheet_name=sheet_name, index=False
            )
        else:
            df.to_excel(writer, sheet_name=sheet_name, index=False)

    def _apply_table_formatting(self, writer: pd.ExcelWriter) -> None:
        """Turn every worksheet into a filterable Excel Table with readable columns."""
        for worksheet in writer.book.worksheets:
            self._format_sheet_as_table(worksheet)

    def _format_sheet_as_table(self, worksheet: Worksheet) -> None:
        """Add an Excel Table, freeze the header, and size/wrap columns."""
        if worksheet.max_row < 2 or worksheet.max_column < 1:
            return

        self._ensure_unique_headers(worksheet)

        max_row = worksheet.max_row
        max_col = worksheet.max_column
        ref = f"A1:{get_column_letter(max_col)}{max_row}"
        table = Table(displayName=self._table_display_name(worksheet.title), ref=ref)
        table.tableStyleInfo = TableStyleInfo(
            name=_TABLE_STYLE,
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        worksheet.add_table(table)
        worksheet.freeze_panes = "A2"
        worksheet.sheet_view.showGridLines = False

        wrap_cols = self._autosize_columns(worksheet)
        self._apply_alignment(worksheet, wrap_cols)
        self._apply_row_heights(worksheet, wrap_cols)

    @staticmethod
    def _table_display_name(sheet_title: str) -> str:
        """Build a unique Excel table name from the sheet title."""
        cleaned = "".join(ch if ch.isalnum() else "_" for ch in sheet_title)
        if not cleaned or not cleaned[0].isalpha():
            cleaned = f"T_{cleaned}"
        return f"tbl{cleaned}"[:255]

    @staticmethod
    def _ensure_unique_headers(worksheet: Worksheet) -> None:
        """Excel Tables require non-empty, unique header cells."""
        seen: dict[str, int] = {}
        for col_idx in range(1, worksheet.max_column + 1):
            cell = worksheet.cell(1, col_idx)
            name = str(cell.value).strip() if cell.value is not None else ""
            if not name:
                name = f"Column_{col_idx}"
            if name in seen:
                seen[name] += 1
                name = f"{name}_{seen[name]}"
            else:
                seen[name] = 1
            cell.value = name

    def _autosize_columns(self, worksheet: Worksheet) -> set[int]:
        """Set column widths from content; return column indexes that should wrap."""
        wrap_cols: set[int] = set()
        for col_idx in range(1, worksheet.max_column + 1):
            header = str(worksheet.cell(1, col_idx).value or "")
            max_len = len(header)
            wrap = header in _WRAP_COLUMNS
            for row_idx in range(1, worksheet.max_row + 1):
                value = worksheet.cell(row_idx, col_idx).value
                if value is None:
                    continue
                cell_len = max((len(line) for line in str(value).splitlines()), default=0)
                max_len = max(max_len, cell_len)
                if cell_len > _MAX_COL_WIDTH:
                    wrap = True
            if wrap:
                wrap_cols.add(col_idx)
            width = min(max(max_len + 2, _MIN_COL_WIDTH), _MAX_COL_WIDTH)
            worksheet.column_dimensions[get_column_letter(col_idx)].width = width
        return wrap_cols

    @staticmethod
    def _apply_alignment(worksheet: Worksheet, wrap_cols: set[int]) -> None:
        """Wrap long text columns and top-align every data cell."""
        for row_idx in range(1, worksheet.max_row + 1):
            for col_idx in range(1, worksheet.max_column + 1):
                worksheet.cell(row_idx, col_idx).alignment = Alignment(
                    wrap_text=col_idx in wrap_cols,
                    vertical="center" if row_idx == 1 else "top",
                    horizontal="left",
                )

    @staticmethod
    def _apply_row_heights(worksheet: Worksheet, wrap_cols: set[int]) -> None:
        """Expand row height so wrapped cells stay readable."""
        if not wrap_cols:
            return
        for row_idx in range(2, worksheet.max_row + 1):
            max_lines = 1
            for col_idx in wrap_cols:
                value = worksheet.cell(row_idx, col_idx).value
                if value is None:
                    continue
                col_width = (
                    worksheet.column_dimensions[get_column_letter(col_idx)].width
                    or _MAX_COL_WIDTH
                )
                lines = 0
                for paragraph in str(value).splitlines() or [""]:
                    lines += max(1, math.ceil(len(paragraph) / max(col_width, 1)))
                max_lines = max(max_lines, min(lines, 6))
            if max_lines > 1:
                worksheet.row_dimensions[row_idx].height = 15 * max_lines

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
