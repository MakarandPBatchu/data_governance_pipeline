"""Execute configurable business rules against Snowflake."""

from __future__ import annotations

from typing import Any

import pandas as pd

from tqdm import tqdm

from src.snowflake_client import SnowflakeClient


class RuleEngine:
    """Run SQL-based business rules defined in ``config/rules.yaml``."""

    def __init__(self, client: SnowflakeClient, settings: dict[str, Any]) -> None:
        """Initialize the engine with a Snowflake client and rule definitions.

        Args:
            client: Connected ``SnowflakeClient`` instance.
            settings: Pipeline settings dict (uses the ``rules`` list).
        """
        self.client = client
        self.settings = settings
        self.rules = [r for r in settings.get("rules", []) if r.get("enabled", True)]

    def run_all(self, database: str, schema: str) -> pd.DataFrame:
        """Execute every enabled, non-dynamic rule and combine the results.

        Args:
            database: Snowflake database name (substituted into rule SQL).
            schema: Schema name (substituted into rule SQL).

        Returns:
            Combined issue DataFrame with DATABASE and SCHEMA columns added.
        """
        all_issues: list[pd.DataFrame] = []

        for rule in tqdm(self.rules, desc="Business rules"):
            if rule.get("dynamic"):
                continue

            df = self.run_rule(rule, database, schema)
            if not df.empty:
                all_issues.append(df)

        if not all_issues:
            return pd.DataFrame()

        combined = pd.concat(all_issues, ignore_index=True)
        return self._standardize_issue_df(combined, database, schema)

    def run_rule(self, rule: dict[str, Any], database: str, schema: str) -> pd.DataFrame:
        """Execute a single business rule and normalize its output columns.

        Rule SQL must return ``ISSUE_TYPE`` and ``ISSUE_DETAIL`` columns.
        On SQL failure, a single ``RULE_ERROR`` row is returned instead of raising.

        Args:
            rule: Rule definition dict from ``rules.yaml``.
            database: Snowflake database name.
            schema: Schema name.

        Returns:
            DataFrame with standard issue columns including ``ROW_IDENTIFIER``.
        """
        sql_template = rule.get("sql", "")
        if not sql_template:
            return pd.DataFrame()

        sql = sql_template.format(database=database, schema=schema)
        try:
            df = self.client.query(sql)
        except Exception as exc:
            return pd.DataFrame(
                [
                    {
                        "TABLE_NAME": rule.get("table", "UNKNOWN"),
                        "ROW_IDENTIFIER": "RULE_ERROR",
                        "ISSUE_TYPE": rule["id"],
                        "ISSUE_DETAIL": f"Rule failed to execute: {exc}",
                        "SEVERITY": rule.get("severity", "medium"),
                        "RULE_NAME": rule.get("name", rule["id"]),
                    }
                ]
            )

        if df.empty:
            return df

        pk_cols = rule.get("primary_key", [])
        df = df.copy()
        df["TABLE_NAME"] = rule.get("table", "UNKNOWN")
        df["PRIMARY_KEY"] = ", ".join(pk_cols) if pk_cols else ""
        df["SEVERITY"] = rule.get("severity", "medium")
        df["RULE_NAME"] = rule.get("name", rule["id"])

        if "ISSUE_TYPE" not in df.columns:
            df["ISSUE_TYPE"] = rule["id"]
        if "ISSUE_DETAIL" not in df.columns:
            df["ISSUE_DETAIL"] = rule.get("description", rule["id"])
            
        if pk_cols:
            df["ROW_IDENTIFIER"] = df[pk_cols].astype(str).agg(" | ".join, axis=1)
        else:
            df["ROW_IDENTIFIER"] = df.index.astype(str)

        return df

    def _standardize_issue_df(self, df: pd.DataFrame, database: str, schema: str) -> pd.DataFrame:
        """Add DATABASE and SCHEMA columns to a combined issues DataFrame."""
        df = df.copy()
        df["DATABASE"] = database
        df["SCHEMA"] = schema
        return df

    def summarize_by_rule(self, issues_df: pd.DataFrame) -> pd.DataFrame:
        """Count issues grouped by issue type, rule name, and severity.

        Args:
            issues_df: Combined profiling and business-rule issue DataFrame.

        Returns:
            Summary DataFrame with an ``ISSUE_COUNT`` column, sorted descending.
        """
        if issues_df.empty:
            return pd.DataFrame(columns=["ISSUE_TYPE", "RULE_NAME", "SEVERITY", "ISSUE_COUNT"])

        group_cols = [c for c in ["ISSUE_TYPE", "RULE_NAME", "SEVERITY"] if c in issues_df.columns]
        summary = (
            issues_df.groupby(group_cols, dropna=False)
            .size()
            .reset_index(name="ISSUE_COUNT")
            .sort_values("ISSUE_COUNT", ascending=False)
        )
        return summary
