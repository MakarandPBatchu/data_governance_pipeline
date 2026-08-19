"""Generic table profiling for data quality metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.snowflake_client import SnowflakeClient


@dataclass
class ColumnProfile:
    """Data quality statistics for a single column."""

    table: str
    column: str
    data_type: str
    null_count: int
    null_rate: float
    distinct_count: int
    issues: list[str] = field(default_factory=list)


@dataclass
class TableProfile:
    """Aggregated profiling results for one Snowflake table."""

    table: str
    row_count: int
    primary_keys: list[str]
    columns: list[ColumnProfile] = field(default_factory=list)
    duplicate_pk_rows: pd.DataFrame = field(default_factory=pd.DataFrame)
    issues: list[str] = field(default_factory=list)


class TableProfiler:
    """Run generic data-quality checks (null rates, duplicate PKs) on Snowflake tables."""

    def __init__(self, client: SnowflakeClient, settings: dict[str, Any]) -> None:
        """Initialize the profiler with a Snowflake client and threshold settings.

        Args:
            client: Connected ``SnowflakeClient`` instance.
            settings: Pipeline settings dict (uses the ``profiling`` section).
        """
        self.client = client
        self.settings = settings
        self.thresholds = settings.get("profiling", {})

    def profile_table(
        self,
        database: str,
        schema: str,
        table: str,
        pk_cols: list[str],
        row_count: int,
    ) -> TableProfile:
        """Profile one table for null rates and duplicate primary-key values.

        Args:
            database: Snowflake database name.
            schema: Schema name.
            table: Table name.
            pk_cols: Primary-key column names for duplicate detection.
            row_count: Pre-fetched row count (avoids an extra COUNT query).

        Returns:
            ``TableProfile`` with per-column stats and any detected issues.
        """
        profile = TableProfile(table=table, row_count=row_count, primary_keys=pk_cols)
        columns_df = self.client.get_columns(database, schema, table)

        if columns_df.empty:
            profile.issues.append("No columns found in INFORMATION_SCHEMA")
            return profile

        null_threshold = float(self.thresholds.get("null_rate_threshold", 0.05))
        table_ref = self.client.format_table_ref(database, schema, table)

        for _, col_row in columns_df.iterrows():
            col_name = col_row["COLUMN_NAME"]
            data_type = col_row["DATA_TYPE"]

            stats_sql = f"""
                SELECT
                    COUNT(1) AS total_rows,
                    COUNT_IF("{col_name}" IS NULL) AS null_count,
                    COUNT(DISTINCT "{col_name}") AS distinct_count
                FROM {table_ref}
            """
            stats = self.client.query(stats_sql).iloc[0]
            total = max(int(stats["TOTAL_ROWS"]), 1)
            null_count = int(stats["NULL_COUNT"])
            null_rate = null_count / total

            col_profile = ColumnProfile(
                table=table,
                column=col_name,
                data_type=data_type,
                null_count=null_count,
                null_rate=null_rate,
                distinct_count=int(stats["DISTINCT_COUNT"]),
            )

            if null_rate > null_threshold:
                col_profile.issues.append(
                    f"Null rate {null_rate:.1%} exceeds threshold {null_threshold:.1%}"
                )
            profile.columns.append(col_profile)

        if pk_cols and row_count > 0:
            pk_list = ", ".join(f'"{c}"' for c in pk_cols)
            dup_sql = f"""
                SELECT {pk_list}, COUNT(1) AS duplicate_count
                FROM {table_ref}
                GROUP BY {pk_list}
                HAVING COUNT(1) > 1
            """
            dup_df = self.client.query(dup_sql)
            if not dup_df.empty:
                profile.duplicate_pk_rows = dup_df
                profile.issues.append(
                    f"Found {len(dup_df)} duplicate primary key group(s) on {pk_cols}"
                )

        return profile

    def profiles_to_issue_rows(
        self,
        profile: TableProfile,
    ) -> pd.DataFrame:
        """Convert profiling results into row-level issue records for the Excel report.

        High-null-rate issues are table-level (``ROW_IDENTIFIER = TABLE_LEVEL``).
        Duplicate-PK issues include the offending key values as ``ROW_IDENTIFIER``.

        Args:
            profile: Completed ``TableProfile`` from ``profile_table``.

        Returns:
            DataFrame of issues ready to merge with business-rule results.
        """
        records: list[dict[str, Any]] = []

        for col in profile.columns:
            if col.issues:
                records.append(
                    {
                        "TABLE_NAME": profile.table,
                        "PRIMARY_KEY": ", ".join(profile.primary_keys),
                        "ROW_IDENTIFIER": "TABLE_LEVEL",
                        "ISSUE_TYPE": "high_null_rate",
                        "ISSUE_DETAIL": f"Column {col.column}: {col.issues[0]}",
                        "SEVERITY": "low",
                        "RULE_NAME": "Column exceeds null rate threshold",
                        "COLUMN_NAME": col.column,
                        "NULL_RATE": col.null_rate,
                    }
                )

        if not profile.duplicate_pk_rows.empty:
            for _, row in profile.duplicate_pk_rows.iterrows():
                pk_values = {
                    pk: row[pk] for pk in profile.primary_keys if pk in profile.duplicate_pk_rows.columns
                }
                identifier = " | ".join(f"{k}={v}" for k, v in pk_values.items())
                records.append(
                    {
                        "TABLE_NAME": profile.table,
                        "PRIMARY_KEY": ", ".join(profile.primary_keys),
                        "ROW_IDENTIFIER": identifier,
                        **pk_values,
                        "ISSUE_TYPE": "duplicate_primary_key",
                        "ISSUE_DETAIL": f"Duplicate PK with count={row.get('DUPLICATE_COUNT', 'N/A')}",
                        "SEVERITY": "critical",
                        "RULE_NAME": "Duplicate primary key values",
                    }
                )

        return pd.DataFrame(records)
