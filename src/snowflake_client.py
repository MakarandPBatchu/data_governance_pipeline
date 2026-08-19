"""Snowflake connection and query utilities."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator, Iterable

import pandas as pd
import snowflake.connector
from snowflake.connector import SnowflakeConnection
from snowflake.connector.cursor import SnowflakeCursor


class SnowflakeClient:
    """Thin wrapper around the Snowflake Python connector for pipeline queries."""

    def __init__(self, settings: dict[str, Any]) -> None:
        """Initialize the client with pipeline settings.

        Args:
            settings: Settings dict from ``load_settings()``.
        """
        self.settings = settings
        self._conn: SnowflakeConnection | None = None

    def connect(self) -> SnowflakeConnection:
        """Open a Snowflake connection (reuses an existing open connection).

        Returns:
            Active ``SnowflakeConnection``.

        Raises:
            ValueError: If account or user is missing from settings.
        """
        if self._conn is not None and not self._conn.is_closed():
            return self._conn

        sf = self.settings["snowflake"]
        if not sf["account"] or not sf["user"]:
            raise ValueError("SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER must be set in .env")

        connect_kwargs: dict[str, Any] = {
            "account": sf["account"],
            "user": sf["user"],
            "password": sf["password"],
            "warehouse": sf["warehouse"],
        }
        if sf.get("database"):
            connect_kwargs["database"] = sf["database"]
        if sf.get("schema"):
            connect_kwargs["schema"] = sf["schema"]
        if sf.get("role"):
            connect_kwargs["role"] = sf["role"]

        self._conn = snowflake.connector.connect(**connect_kwargs)
        return self._conn

    def close(self) -> None:
        """Close the Snowflake connection if one is open."""
        if self._conn and not self._conn.is_closed():
            self._conn.close()
        self._conn = None

    @contextmanager
    def cursor(self) -> Generator[SnowflakeCursor, None, None]:
        """Yield a cursor that is automatically closed when the block exits."""
        conn = self.connect()
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def query(self, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        """Execute SQL and return all rows as a DataFrame.

        Args:
            sql: SQL statement (supports ``%(name)s`` parameter placeholders).
            params: Optional bind parameters for the query.

        Returns:
            Query results with column names taken from the cursor description.
        """
        with self.cursor() as cur:
            cur.execute(sql, params or {})
            columns = [col[0] for col in cur.description] if cur.description else []
            rows = cur.fetchall()
        return pd.DataFrame(rows, columns=columns)

    def query_scalar(self, sql: str) -> Any:
        """Execute SQL and return the first column of the first row.

        Args:
            sql: SQL statement returning a single value.

        Returns:
            Scalar result, or ``None`` if no rows are returned.
        """
        with self.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return row[0] if row else None

    def list_tables(self, database: str, schema: str) -> list[str]:
        """List base tables in a schema, excluding configured table names.

        Args:
            database: Snowflake database (catalog) name.
            schema: Schema name within the database.

        Returns:
            Sorted list of table names to scan.
        """
        sql = """
            SELECT TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_CATALOG = %(database)s
              AND TABLE_SCHEMA = %(schema)s
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
        """
        df = self.query(sql, {"database": database.upper(), "schema": schema.upper()})
        exclude = {name.upper() for name in self.settings.get("exclude_tables", [])}
        return [t for t in df["TABLE_NAME"].tolist() if t.upper() not in exclude]

    def get_columns(self, database: str, schema: str, table: str) -> pd.DataFrame:
        """Fetch column metadata for a table from INFORMATION_SCHEMA.

        Args:
            database: Snowflake database name.
            schema: Schema name.
            table: Table name.

        Returns:
            DataFrame with COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION.
        """
        sql = """
            SELECT
                COLUMN_NAME,
                DATA_TYPE,
                IS_NULLABLE,
                ORDINAL_POSITION
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_CATALOG = %(database)s
              AND TABLE_SCHEMA = %(schema)s
              AND TABLE_NAME = %(table)s
            ORDER BY ORDINAL_POSITION
        """
        return self.query(
            sql,
            {"database": database.upper(), "schema": schema.upper(), "table": table.upper()},
        )

    def get_primary_keys(self, database: str, schema: str, table: str) -> list[str]:
        """Resolve primary-key column names for a table.

        Resolution order:
        1. Manual override from ``config.yaml`` ``table_primary_keys``
        2. ``SHOW PRIMARY KEYS IN TABLE`` (Snowflake-native)
        3. Heuristic fallback via ``_infer_primary_keys``

        Args:
            database: Snowflake database name.
            schema: Schema name.
            table: Table name.

        Returns:
            Ordered list of PK column names (may be empty if the table has no columns).
        """
        overrides = self.settings.get("table_primary_keys", {})
        for key, value in overrides.items():
            if key.upper() == table.upper():
                return value

        try:
            with self.cursor() as cur:
                cur.execute(
                    f'SHOW PRIMARY KEYS IN TABLE "{database}"."{schema}"."{table}"'
                )
                if not cur.description:
                    return self._infer_primary_keys(database, schema, table)

                columns = [col[0] for col in cur.description]
                rows = cur.fetchall()
                if not rows:
                    return self._infer_primary_keys(database, schema, table)

                df = pd.DataFrame(rows, columns=columns)
                col_field = next(
                    (c for c in df.columns if c.lower() == "column_name"),
                    None,
                )
                seq_field = next(
                    (c for c in df.columns if c.lower() == "key_sequence"),
                    None,
                )
                if col_field:
                    if seq_field:
                        df = df.sort_values(seq_field)
                    return df[col_field].tolist()
        except Exception:
            pass

        return self._infer_primary_keys(database, schema, table)

    def _infer_primary_keys(self, database: str, schema: str, table: str) -> list[str]:
        """Guess PK columns when no constraint is defined (common in test/messy schemas).

        Prefers columns ending in ``ID`` or ``KEY``, otherwise uses the first column.

        Args:
            database: Snowflake database name.
            schema: Schema name.
            table: Table name.

        Returns:
            Single-element list with the best-guess PK column, or empty list.
        """
        columns = self.get_columns(database, schema, table)
        for candidate, is_nullable in zip(columns["COLUMN_NAME"], columns["IS_NULLABLE"]):
            upper = candidate.upper()
            if (upper.endswith("ID") or upper.endswith("KEY")) and is_nullable == "NO":
                return [candidate]
        col_names = columns["COLUMN_NAME"].tolist()
        return [col_names[0]] if col_names else []

    def get_row_count(self, database: str, schema: str, table: str) -> int:
        """Return the total row count for a table.

        Args:
            database: Snowflake database name.
            schema: Schema name.
            table: Table name.

        Returns:
            Number of rows (0 if the table is empty).
        """
        sql = f'SELECT COUNT(1) FROM "{database}"."{schema}"."{table}"'
        result = self.query_scalar(sql)
        return int(result or 0)

    def format_table_ref(self, database: str, schema: str, table: str) -> str:
        """Return a fully qualified, quoted table reference for use in SQL.

        Args:
            database: Snowflake database name.
            schema: Schema name.
            table: Table name.

        Returns:
            Quoted identifier string, e.g. ``"DB"."SCHEMA"."TABLE"``.
        """
        return f'"{database}"."{schema}"."{table}"'

    def iter_table_batches(
        self,
        database: str,
        schema: str,
        tables: Iterable[str],
    ) -> Iterable[tuple[str, list[str], int]]:
        """Yield profiling metadata for each table one at a time.

        Args:
            database: Snowflake database name.
            schema: Schema name.
            tables: Iterable of table names to inspect.

        Yields:
            Tuples of ``(table_name, primary_key_columns, row_count)``.
        """
        for table in tables:
            pk_cols = self.get_primary_keys(database, schema, table)
            row_count = self.get_row_count(database, schema, table)
            yield table, pk_cols, row_count
