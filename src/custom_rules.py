"""Load natural-language custom rules, generate SQL via Ollama, and execute them."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

from src.llm_analyzer import LlamaAnalyzer
from src.rule_engine import RuleEngine
from src.snowflake_client import SnowflakeClient

logger = logging.getLogger(__name__)

_FORBIDDEN_SQL = re.compile(
    r"(?is)\b(?:INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|"
    r"COPY\s+INTO|PUT|GET|CALL|UNDROP|BEGIN|COMMIT|ROLLBACK|EXECUTE|REMOVE)\b"
)
_REQUIRED_COLUMNS = ("id", "name", "table", "rule_text")
_TRUE_VALUES = {"true", "1", "yes", "y"}
_SEVERITIES = {"critical", "high", "medium", "low"}


class CustomRuleRunner:
    """Turn CSV natural-language rules into Snowflake issue rows via the LLM."""

    def __init__(
        self,
        client: SnowflakeClient,
        llm: LlamaAnalyzer,
        rule_engine: RuleEngine,
        settings: dict[str, Any],
    ) -> None:
        """Initialize with the same Snowflake, LLM, and rule-engine components as the pipeline.

        Args:
            client: Connected ``SnowflakeClient``.
            llm: Ollama analyzer used to generate SQL.
            rule_engine: Existing engine used to execute generated SQL.
            settings: Pipeline settings (uses ``project_root``).
        """
        self.client = client
        self.llm = llm
        self.rule_engine = rule_engine
        self.settings = settings
        self.tables_targeted = 0
        self.targeted_table_names: list[str] = []

    def run(
        self,
        path: str | Path,
        database: str,
        schema: str,
        available_tables: list[str],
    ) -> pd.DataFrame:
        """Load a custom-rules CSV, generate SQL, execute each rule, and combine issues.

        Args:
            path: Path to the CSV template (relative paths resolve from project root).
            database: Snowflake database name.
            schema: Schema name.
            available_tables: Base tables in the target schema (from ``list_tables``).

        Returns:
            Combined issue DataFrame with DATABASE and SCHEMA columns, or empty.

        Raises:
            FileNotFoundError: If the CSV path does not exist.
            ValueError: If the CSV is missing required columns.
        """
        csv_path = self._resolve_path(path)
        rows = self.load_csv(csv_path)
        targeted = [r["table"] for r in rows] if rows else []
        self.targeted_table_names = list(dict.fromkeys(targeted))
        self.tables_targeted = len(self.targeted_table_names)

        if not rows:
            logger.warning("No enabled custom rules found in %s", csv_path)
            return pd.DataFrame()

        known_tables = {t.upper() for t in available_tables}
        all_issues: list[pd.DataFrame] = []

        for row in tqdm(rows, desc="Custom rules"):
            df = self._run_one(row, database, schema, known_tables)
            if not df.empty:
                all_issues.append(df)

        if not all_issues:
            return pd.DataFrame()

        combined = pd.concat(all_issues, ignore_index=True).copy()
        combined["DATABASE"] = database
        combined["SCHEMA"] = schema
        return combined

    def load_csv(self, path: Path) -> list[dict[str, Any]]:
        """Parse the custom-rules CSV into normalized rule dicts.

        Args:
            path: Existing CSV file path.

        Returns:
            Enabled rule rows ready for SQL generation.

        Raises:
            ValueError: If required columns are missing.
        """
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

        missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(
                f"Custom rules CSV is missing required columns: {', '.join(missing)}. "
                "Expected: id, name, table, primary_key, severity, enabled, "
                "related_tables, rule_text."
            )

        rules: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for idx, raw in df.iterrows():
            if not self._as_bool(raw.get("enabled", "true"), default=True):
                continue

            rule_id = str(raw.get("id", "")).strip()
            name = str(raw.get("name", "")).strip()
            table = str(raw.get("table", "")).strip()
            rule_text = str(raw.get("rule_text", "")).strip()

            if not rule_id or not name or not table or not rule_text:
                logger.warning(
                    "Skipping custom rules CSV row %s: id, name, table, and rule_text are required",
                    int(idx) + 2,
                )
                continue

            if rule_id in seen_ids:
                raise ValueError(f"Duplicate custom rule id '{rule_id}' in {path}")
            seen_ids.add(rule_id)

            severity = str(raw.get("severity", "medium")).strip().lower() or "medium"
            if severity not in _SEVERITIES:
                logger.warning(
                    "Custom rule '%s' has invalid severity '%s'; defaulting to medium",
                    rule_id,
                    severity,
                )
                severity = "medium"

            rules.append(
                {
                    "id": rule_id,
                    "name": name,
                    "table": table,
                    "primary_key": self._split_names(raw.get("primary_key", "")),
                    "severity": severity,
                    "related_tables": self._split_names(raw.get("related_tables", "")),
                    "rule_text": rule_text,
                }
            )

        return rules

    def _run_one(
        self,
        row: dict[str, Any],
        database: str,
        schema: str,
        known_tables: set[str],
    ) -> pd.DataFrame:
        """Generate and execute a single custom rule, or return a RULE_ERROR row."""
        table = row["table"]
        missing_tables = [
            name for name in [table, *row["related_tables"]] if name.upper() not in known_tables
        ]
        if missing_tables:
            return self._error_row(
                row,
                f"Table(s) not found in target schema: {', '.join(missing_tables)}",
            )

        try:
            schema_context, schema_columns = self._schema_context(
                database, schema, [table, *row["related_tables"]]
            )
        except Exception as exc:
            return self._error_row(row, f"Failed to read table metadata: {exc}")

        required_select = infer_required_select_columns(
            row["rule_text"], row["primary_key"], schema_columns
        )
        where_notes = infer_where_coverage_notes(row["rule_text"])
        logger.info(
            "Custom rule '%s' SELECT checklist: %s",
            row["id"],
            ", ".join(required_select) or "(none)",
        )

        sql = None
        last_error: str | None = None
        for attempt in range(3):
            try:
                sql = self.llm.generate_rule_sql(
                    rule_id=row["id"],
                    rule_name=row["name"],
                    rule_text=row["rule_text"],
                    table=table,
                    primary_key=row["primary_key"],
                    schema_context=schema_context,
                    previous_error=last_error,
                    required_select_columns=required_select,
                    where_coverage_notes=where_notes,
                )
            except Exception as exc:
                last_error = f"LLM SQL generation failed: {exc}"
                logger.warning("Custom rule '%s' generation attempt %s failed: %s", row["id"], attempt + 1, exc)
                continue

            sql = qualify_generated_sql(
                sql,
                tables=[table, *row["related_tables"]],
                database=database,
                schema=schema,
            )
            sql = rebuild_select_list(sql, required_select, row["id"])
            sql = ensure_issue_aliases(sql, row["id"])
            logger.info("Custom rule '%s' candidate SQL (attempt %s):\n%s", row["id"], attempt + 1, sql)

            last_error = validate_generated_sql(sql)
            if last_error is None:
                last_error = validate_sql_covers_rule(sql, required_select, row["rule_text"])
            if last_error is None:
                break
            logger.warning(
                "Custom rule '%s' generated SQL failed validation (attempt %s): %s",
                row["id"],
                attempt + 1,
                last_error,
            )
            sql = None

        if last_error or not sql:
            return self._error_row(row, last_error or "LLM did not return valid SQL")

        logger.info("Custom rule '%s' generated SQL:\n%s", row["id"], sql)

        rule = {
            "id": row["id"],
            "name": row["name"],
            "description": row["rule_text"],
            "table": table,
            "primary_key": row["primary_key"],
            "severity": row["severity"],
            "sql": sql,
        }
        return self.rule_engine.run_rule(rule, database, schema)

    def _schema_context(self, database: str, schema: str, tables: list[str]) -> tuple[str, list[str]]:
        """Build a compact column listing for the LLM prompt.

        Returns:
            Tuple of (schema text for the prompt, distinct column names).
        """
        blocks: list[str] = []
        column_names: list[str] = []
        seen_tables: set[str] = set()
        seen_cols: set[str] = set()
        for table in tables:
            key = table.upper()
            if key in seen_tables:
                continue
            seen_tables.add(key)
            cols = self.client.get_columns(database, schema, table)
            if cols.empty:
                blocks.append(f"{table}: (no columns found in INFORMATION_SCHEMA)")
                continue
            lines = [f"{table}:"]
            for _, col in cols.iterrows():
                name = str(col["COLUMN_NAME"])
                nullable = "NULL" if str(col["IS_NULLABLE"]).upper() == "YES" else "NOT NULL"
                lines.append(f"  - {name} {col['DATA_TYPE']} {nullable}")
                if name.upper() not in seen_cols:
                    seen_cols.add(name.upper())
                    column_names.append(name)
            blocks.append("\n".join(lines))
        return "\n".join(blocks), column_names

    def _resolve_path(self, path: str | Path) -> Path:
        """Resolve a custom-rules path against the project root when it is relative."""
        resolved = Path(path)
        if not resolved.is_absolute():
            root = Path(self.settings["project_root"])
            resolved = root / resolved
        if not resolved.is_file():
            raise FileNotFoundError(f"Custom rules file not found: {resolved}")
        return resolved

    @staticmethod
    def _error_row(row: dict[str, Any], detail: str) -> pd.DataFrame:
        """Build a single RULE_ERROR issue row that matches RuleEngine failures."""
        return pd.DataFrame(
            [
                {
                    "TABLE_NAME": row.get("table", "UNKNOWN"),
                    "ROW_IDENTIFIER": "RULE_ERROR",
                    "ISSUE_TYPE": row["id"],
                    "ISSUE_DETAIL": f"Rule failed to execute: {detail}",
                    "SEVERITY": row.get("severity", "medium"),
                    "RULE_NAME": row.get("name", row["id"]),
                }
            ]
        )

    @staticmethod
    def _split_names(value: Any) -> list[str]:
        """Split a comma/pipe-separated identifier list."""
        if value is None:
            return []
        text = str(value).strip()
        if not text:
            return []
        parts = re.split(r"[,|;]+", text)
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _as_bool(value: Any, default: bool = True) -> bool:
        """Parse common CSV boolean strings; blank values use ``default``."""
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {"", "nan"}:
            return default
        return text in _TRUE_VALUES


def qualify_generated_sql(sql: str, tables: list[str], database: str, schema: str) -> str:
    """Prefix FROM/JOIN tables with ``{database}.{schema}.`` after LLM generation.

    Llama JSON mode often omits or mangles curly-brace placeholders, so qualification
    is applied here instead of asking the model to emit them.
    """
    text = sql.strip()
    text = re.sub(r"\{database\}\.\{schema\}\.", "", text, flags=re.IGNORECASE)
    text = re.sub(rf"(?i)\b{re.escape(database)}\.{re.escape(schema)}\.", "", text)
    text = re.sub(
        rf'(?i)"{re.escape(database)}"\s*\.\s*"{re.escape(schema)}"\s*\.\s*',
        "",
        text,
    )
    for table in sorted({t for t in tables if t}, key=len, reverse=True):
        text = re.sub(
            rf'(?is)\b(FROM|JOIN)\s+(?:[\w]+\.[\w]+\.)?(?:"?{re.escape(table)}"?)\b',
            rf"\1 {{database}}.{{schema}}.{table}",
            text,
        )
    return text


def ensure_issue_aliases(sql: str, rule_id: str) -> str:
    """Add ISSUE_TYPE / ISSUE_DETAIL aliases when the LLM omitted them."""
    text = sql
    if not re.search(r"(?i)\bISSUE_TYPE\b", text):
        text, n = re.subn(
            rf"(?i)('{re.escape(rule_id)}')(?!\s+AS\s+ISSUE_TYPE)",
            r"\1 AS ISSUE_TYPE",
            text,
            count=1,
        )
        if n == 0:
            text = re.sub(
                r"(?i)\s+FROM\b",
                f", '{rule_id}' AS ISSUE_TYPE FROM",
                text,
                count=1,
            )
    if not re.search(r"(?i)\bISSUE_DETAIL\b", text):
        text = re.sub(
            r"(?i)\s+FROM\b",
            f", '{rule_id}' AS ISSUE_DETAIL FROM",
            text,
            count=1,
        )
    return text


def rebuild_select_list(sql: str, required_select: list[str], rule_id: str) -> str:
    """Replace the SELECT list with required columns plus ISSUE_TYPE / ISSUE_DETAIL.

    Llama often drops include-columns or emits invalid aliases; FROM/JOIN/WHERE are kept.
    """
    from_part = re.search(r"(?is)\bFROM\b[\s\S]*", sql)
    if not from_part:
        return sql
    detail_match = re.search(r"(?is)('[^']*'\s+AS\s+ISSUE_DETAIL)", sql)
    detail = detail_match.group(1).strip() if detail_match else f"'{rule_id}' AS ISSUE_DETAIL"
    cols = ", ".join(required_select) if required_select else "*"
    return f"SELECT {cols}, '{rule_id}' AS ISSUE_TYPE, {detail} {from_part.group(0)}"


def _norm_ident(value: str) -> str:
    """Uppercase identifier with underscores and spaces removed."""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _sql_clause(sql: str, start: str, end: str | None) -> str:
    """Return the SQL text between two keywords (end exclusive)."""
    pattern = rf"(?is)\b{start}\b([\s\S]+?)"
    pattern += rf"\b{end}\b" if end else r"$"
    match = re.search(pattern, sql)
    return match.group(1) if match else ""


def infer_required_select_columns(
    rule_text: str,
    primary_key: list[str],
    schema_columns: list[str],
) -> list[str]:
    """Map 'include X, Y, Z in the result' (plus PKs) onto real schema column names."""
    required: list[str] = []
    seen: set[str] = set()

    def add(column: str) -> None:
        key = column.upper()
        if key in seen:
            return
        seen.add(key)
        required.append(column)

    for pk in primary_key:
        add(pk)

    include_match = re.search(
        r"(?is)\binclude\b(.+?)(?:\bin the result\b|\.|$)",
        rule_text,
    )
    haystack = include_match.group(1) if include_match else ""
    if not haystack.strip():
        return required

    hay_upper = haystack.upper()
    hay_norm = _norm_ident(haystack)
    for col in sorted(schema_columns, key=len, reverse=True):
        if col.upper() in hay_upper or _norm_ident(col) in hay_norm:
            add(col)
    return required


def infer_where_coverage_notes(rule_text: str) -> list[str]:
    """Describe WHERE completeness rules implied by the natural-language text."""
    text = rule_text.lower()
    notes: list[str] = []
    if re.search(r"\bnulls?\b", text):
        notes.append(
            "Rule mentions null: WHERE must include IS NULL ORed with the other "
            "alternatives. Comparisons such as <= 0 do not match NULL."
        )
    if re.search(r"\bzero\b", text):
        notes.append("Rule mentions zero: WHERE must include = 0 or <= 0.")
    if re.search(r"\bnegative\b", text):
        notes.append("Rule mentions negative: WHERE must include < 0 or <= 0.")
    if re.search(r"\bnon-?deleted\b|\bnot deleted\b", text):
        notes.append(
            "Rule mentions non-deleted: WHERE must use COALESCE(IS_DELETED, FALSE) = FALSE."
        )
    for match in re.finditer(
        r"\b([A-Za-z][A-Za-z0-9_]*)\s*=\s*(FALSE|TRUE)\b",
        rule_text,
        re.IGNORECASE,
    ):
        notes.append(
            f"WHERE must include {match.group(1).upper()} = {match.group(2).upper()}."
        )
    return notes


def validate_sql_covers_rule(
    sql: str,
    required_select: list[str],
    rule_text: str,
) -> str | None:
    """Return an error if SELECT/WHERE omit cases implied by the rule text."""
    select_sql = _sql_clause(sql, "SELECT", "FROM").upper()
    where_sql = _sql_clause(sql, "WHERE", None).upper()
    missing_select = [
        col
        for col in required_select
        if not re.search(rf"\b{re.escape(col.upper())}\b", select_sql)
    ]
    if missing_select:
        return (
            "SELECT is missing columns the rule asked to include: "
            + ", ".join(missing_select)
        )

    text = rule_text.lower()
    gaps: list[str] = []
    if re.search(r"\bnulls?\b", text) and not re.search(r"\bIS\s+NULL\b", where_sql):
        gaps.append("IS NULL (null case; comparisons do not match NULL)")
    if re.search(r"\bzero\b", text) and not re.search(r"(<=\s*0|=\s*0(\.0+)?)", where_sql):
        gaps.append("= 0 or <= 0 (zero case)")
    if re.search(r"\bnegative\b", text) and not re.search(r"(<=\s*0|<\s*0)", where_sql):
        gaps.append("< 0 or <= 0 (negative case)")
    if re.search(r"\bnon-?deleted\b|\bnot deleted\b", text) and not re.search(
        r"\bIS_DELETED\b", where_sql
    ):
        gaps.append("IS_DELETED filter for non-deleted rows")
    for match in re.finditer(
        r"\b([A-Za-z][A-Za-z0-9_]*)\s*=\s*(FALSE|TRUE)\b",
        rule_text,
        re.IGNORECASE,
    ):
        col, val = match.group(1).upper(), match.group(2).upper()
        if not re.search(rf"\b{re.escape(col)}\b", where_sql) or val not in where_sql:
            gaps.append(f"{col} = {val}")
    if gaps:
        return "WHERE is missing required coverage: " + "; ".join(gaps)
    return None


def validate_generated_sql(sql: str) -> str | None:
    """Return a validation error message, or ``None`` if the SQL is safe to run.

    Args:
        sql: Candidate Snowflake statement from the LLM.

    Returns:
        Error string when the SQL is rejected; ``None`` when it may be executed.
    """
    if not sql or not str(sql).strip():
        return "SQL was empty"

    cleaned = str(sql).strip().rstrip(";").strip()
    if not cleaned:
        return "SQL was empty"
    if ";" in cleaned:
        return "SQL must be a single statement (no semicolons)"

    first = re.match(r"(?is)(?:\s*--[^\n]*\n|\s*/\*.*?\*/|\s+)*(\w+)", cleaned)
    keyword = first.group(1).upper() if first else ""
    if keyword not in {"SELECT", "WITH"}:
        return f"SQL must start with SELECT or WITH, found '{keyword or 'nothing'}'"

    if _FORBIDDEN_SQL.search(cleaned):
        return "SQL contains a forbidden keyword (only SELECT / WITH queries are allowed)"

    if "{database}" not in cleaned or "{schema}" not in cleaned:
        return "SQL must use {database} and {schema} placeholders, not hard-coded names"
    extra = [
        name
        for name in re.findall(r"\{([^{}]+)\}", cleaned)
        if name not in {"database", "schema"}
    ]
    if extra:
        return f"SQL contains unsupported placeholders: {', '.join(sorted(set(extra)))}"

    upper_sql = cleaned.upper()
    if "ISSUE_TYPE" not in upper_sql:
        return "SQL must select an ISSUE_TYPE column"
    if "ISSUE_DETAIL" not in upper_sql:
        return "SQL must select an ISSUE_DETAIL column"

    return None
