"""Analyze data quality findings using local Llama 3.2 via Ollama."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import ollama
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_META_SKIP_COLUMNS = frozenset({"DATABASE", "SCHEMA", "LLM_SUMMARY"})
_FACT_COLUMN_ORDER = (
    "TABLE_NAME",
    "ISSUE_TYPE",
    "RULE_NAME",
    "SEVERITY",
    "ROW_IDENTIFIER",
    "PRIMARY_KEY",
    "ISSUE_DETAIL",
    "COLUMN_NAME",
    "NULL_RATE",
)
_FACT_LABELS = {
    "TABLE_NAME": "Table",
    "ISSUE_TYPE": "Issue type",
    "RULE_NAME": "Issue name",
    "SEVERITY": "Severity",
    "ROW_IDENTIFIER": "Affected record",
    "PRIMARY_KEY": "Primary key",
    "ISSUE_DETAIL": "Finding",
    "COLUMN_NAME": "Column",
    "NULL_RATE": "Null rate",
}
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_CODE_RE = re.compile(r"\b[A-Z]{1,8}\d+\b", re.IGNORECASE)
_ISSUE_AS_ID_RE = re.compile(
    r"\bissue\s*(?:\(|:|-)?\s*[A-Z]{1,8}\d+\b",
    re.IGNORECASE,
)
_CURRENCY_RE = re.compile(
    r"(?i)[$£€¥₹₩₽₪₫₱₡₦₴₵₸₺₼₾฿元円]|\b(?:USD|GBP|EUR|JPY|INR|AUD|CAD|CHF|CNY)\b"
)


class LlamaAnalyzer:
    """Use a local Ollama model to generate custom-rule SQL and enrich findings."""

    def __init__(self, settings: dict[str, Any]) -> None:
        """Initialize the Ollama client from pipeline LLM settings.

        Args:
            settings: Pipeline settings dict (uses ``llm`` and ``profiling`` sections).
        """
        llm_cfg = settings["llm"]
        self.model = llm_cfg["model"]
        self.host = llm_cfg["host"]
        self.temperature = llm_cfg.get("temperature", 0.1)
        self.max_tokens = llm_cfg.get("max_tokens", 1024)
        self._client = ollama.Client(host=self.host)

    def verify_connection(self) -> None:
        """Confirm Ollama is reachable and the configured model is available.

        Raises:
            ConnectionError: If the model is not found in the local Ollama instance.
        """
        models = self._client.list()
        available = [m.model for m in models.models]
        if not any(self.model.split(":")[0] in name for name in available):
            raise ConnectionError(
                f"Model '{self.model}' not found in Ollama. Available: {available}. "
                f"Run: ollama pull {self.model}"
            )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _chat(
        self,
        prompt: str,
        max_tokens: int | None = None,
        *,
        json_mode: bool = False,
    ) -> str:
        """Send a prompt to Ollama and return the model's text response.

        Retries up to 3 times with exponential backoff on transient failures.

        Args:
            prompt: User message content.
            max_tokens: Override for tokens to generate; defaults to settings.
            json_mode: When True, ask Ollama to emit a JSON object.
        Returns:
            Stripped text content from the model response.
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "options": {
                "temperature": self.temperature,
                "num_predict": max_tokens if max_tokens is not None else self.max_tokens,
            },
        }
        if json_mode:
            kwargs["format"] = "json"
        response = self._client.chat(**kwargs)
        return response.message.content.strip()

    def analyze_table_profile(
        self,
        table_name: str,
        row_count: int,
        column_issues: list[str],
        table_issues: list[str],
    ) -> str:
        """Generate a short narrative summary of a table's data-quality profile.

        Args:
            table_name: Snowflake table name.
            row_count: Total rows in the table.
            column_issues: Human-readable column-level issue descriptions.
            table_issues: Human-readable table-level issue descriptions.

        Returns:
            2–3 sentence prose summary from the LLM.
        """
        prompt = f"""You are a data governance analyst. Summarize data quality for the table - '{table_name}'.

Row count of the table: {row_count}
Column-level issues found in the table:
{chr(10).join(f'- {i}' for i in column_issues) or '- None'}

Table-level issues found in the table:
{chr(10).join(f'- {i}' for i in table_issues) or '- None'}

Write 2-3 sentences about the data quality issues found in the table for a governance report.
Be specific and descriptive about the issues found. No bullet points."""

        return self._chat(prompt)

    def generate_rule_sql(
        self,
        *,
        rule_id: str,
        rule_name: str,
        rule_text: str,
        table: str,
        primary_key: list[str],
        schema_context: str,
        previous_error: str | None = None,
        required_select_columns: list[str] | None = None,
        where_coverage_notes: list[str] | None = None,
    ) -> str:
        """Ask the LLM to turn a natural-language rule into Snowflake SELECT SQL.

        Args:
            rule_id: Stable issue-type identifier written into ``ISSUE_TYPE``.
            rule_name: Human-readable rule name (context only).
            rule_text: Free-text description of the data-quality check.
            table: Primary Snowflake table the rule targets.
            primary_key: Primary-key column names that the SQL must return.
            schema_context: Column metadata for the primary and related tables.
            previous_error: Validator error from a prior attempt, if retrying.
            required_select_columns: Schema columns that must appear in SELECT.
            where_coverage_notes: Completeness rules for WHERE (null / zero / etc.).

        Returns:
            SQL template using ``{database}`` and ``{schema}`` placeholders.

        Raises:
            ValueError: If the model response cannot be parsed as SQL.
        """
        pk_list = ", ".join(primary_key) if primary_key else "(none provided)"
        retry_block = ""
        if previous_error:
            retry_block = f"""
The previous SQL was rejected for this reason:
{previous_error}

Fix the SQL. Do not repeat the same mistake. Keep every SELECT column and every WHERE alternative from the checklist.
"""

        select_lines = "\n".join(
            f"- {c}" for c in (required_select_columns or primary_key or [])
        ) or "- (primary key only)"
        where_lines = "\n".join(f"- {n}" for n in (where_coverage_notes or [])) or (
            "- (no extra coverage notes)"
        )

        prompt = f"""You are a Snowflake SQL generator for a data governance pipeline.
Convert the natural-language data quality rule into a single Snowflake SELECT query.
{retry_block}
Primary table: {table}
Primary key columns: {pk_list}
Rule id (use this exact literal for ISSUE_TYPE): {rule_id}
Rule name: {rule_name}

Available schema (only use these tables and columns):
{schema_context}

Natural-language rule:
{rule_text}

SELECT checklist — every one of these columns MUST appear in the SELECT list
(map informal names such as "adviser name" to the matching schema column):
{select_lines}

WHERE checklist — the query must match EVERY case the rule describes, not a subset:
{where_lines}

SQL three-valued logic (do not skip this):
- NULL is not equal to 0, not less than 0, and not FALSE. If the rule includes a null case, you MUST add "column IS NULL" and OR it with the other alternatives.
- If the rule lists alternatives with commas or "or" (for example null, zero, or negative), the WHERE clause must include a predicate for each alternative, combined with OR. Do not keep only one of them.
- Zero and negative numbers can be combined as "column <= 0", but that still does not match NULL.
- BOOLEAN comparisons use TRUE / FALSE, not quoted strings.
- "Non-deleted" means COALESCE(IS_DELETED, FALSE) = FALSE.

Hard requirements:
- Snowflake SQL dialect only.
- A single SELECT or WITH ... SELECT statement. No DML or DDL.
- Use unqualified table names only (for example FROM CLIENTS c JOIN ADVISERS a). Do not add a database or schema prefix. Do not use curly braces.
- MUST also select '{rule_id}' AS ISSUE_TYPE and a human-readable ISSUE_DETAIL for THIS rule.
- JOIN related tables only when the rule needs columns or matches from them.
- Only reference tables and columns listed in the schema above.
- Put the SQL on one line. No trailing semicolon, no extra quotes around the query.

Respond with a JSON object whose sql value is a single-line string:
{{"sql": "SELECT ... FROM CLIENTS c WHERE ..."}}"""

        raw = self._chat(prompt, max_tokens=2048, json_mode=True)
        return self._parse_sql_response(raw)

    def _parse_sql_response(self, raw: str) -> str:
        """Extract a SQL string from JSON or a fenced SQL block."""
        try:
            parsed = json.loads(raw.strip())
            if isinstance(parsed, dict) and parsed.get("sql"):
                return self._clean_generated_sql(str(parsed["sql"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        extracted = self._extract_sql_field(raw)
        if extracted:
            return extracted

        fence = re.search(r"```(?:sql)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
        if fence:
            candidate = self._clean_generated_sql(fence.group(1))
            if candidate:
                return candidate

        select_match = re.search(r"(?is)\b(WITH|SELECT)\b[\s\S]+", raw)
        if select_match:
            return self._clean_generated_sql(select_match.group(0))

        raise ValueError("LLM did not return parseable SQL.")

    def _extract_sql_field(self, raw: str) -> str | None:
        """Pull the sql field out of near-JSON when json.loads fails (unescaped newlines)."""
        match = re.search(r'"sql"\s*:\s*"(.*)"\s*[,}]', raw, re.DOTALL | re.IGNORECASE)
        if not match:
            return None
        sql = match.group(1).replace(r"\"", '"').replace(r"\n", "\n").replace(r"\t", "\t")
        cleaned = self._clean_generated_sql(sql)
        return cleaned or None

    @staticmethod
    def _clean_generated_sql(sql: str) -> str:
        """Strip markdown leftovers, wrapping quotes, and trailing JSON artifacts."""
        text = sql.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:sql)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1].strip()
        text = text.rstrip(";").strip()
        if text.endswith('"') and text.count('"') % 2 == 1:
            text = text[:-1].strip()
        while text.endswith("}") and not (
            text.endswith("{database}") or text.endswith("{schema}")
        ):
            text = text[:-1].rstrip()
        return text

    def enrich_issues(self, issues_df: pd.DataFrame) -> pd.DataFrame:
        """Add a grounded one-sentence LLM summary for each issue row.

        Each row is summarized from only that row's fields. Summaries that
        introduce identifiers or numbers not present in the row are discarded.

        Args:
            issues_df: Combined issue DataFrame from profiling and business rules.

        Returns:
            Copy of ``issues_df`` with an ``LLM_SUMMARY`` column added.
        """
        if issues_df.empty:
            return issues_df

        enriched = issues_df.copy()
        enriched["LLM_SUMMARY"] = ""
        elapsed: list[float] = []

        for idx, row in enriched.iterrows():
            ident = row.get("ROW_IDENTIFIER") or row.get("CLIENT_ID") or idx
            logger.info("Summarizing issue row %s (%s)", ident, row.get("ISSUE_TYPE", ""))
            start_time = time.time()
            enriched.at[idx, "LLM_SUMMARY"] = self._summarize_issue_row(row)
            elapsed.append(time.time() - start_time)

        if elapsed:
            logger.info(
                "Average time taken by LLM per issue row: %.2f seconds",
                sum(elapsed) / len(elapsed),
            )
        return enriched

    def _summarize_issue_row(self, row: pd.Series) -> str:
        """Paraphrase one issue row; fall back to the row's own detail if ungrounded."""
        facts = self._issue_row_facts(row)
        fallback = self._fallback_row_summary(row)
        if not facts:
            return fallback

        prompt = f"""You are a data governance analyst.
Rewrite the finding below as one sentence for a governance report.

Use ONLY these facts. Copy identifiers, amounts, dates, codes, and rates exactly.
"Issue name" / "Issue type" is the name of the problem. The "Affected record" value
is the row that failed (a client id, portfolio id, etc.). Never treat that value as
the issue name. Do not write "the issue C003" or "issue (C003)".
Use the Affected record as the subject, for example "Record PF_DUP1 has ..." or
"Client C002 has ...". Mention a client id only if it is this row's Affected record.
"Finding" is the source of truth for what is wrong on this row.
Do not invent, round, convert, or guess any value that is not listed.
Do not mention any other client, table, column, or statistic.
Do not say a field is missing or invalid unless the Finding says so.
Do not add business impact, recommendations, timestamps, or placeholders such as [DATE].
Write amounts such as AUM as plain numbers only. Do not add currency symbols
($, £, €) or currency codes (USD, GBP).

Facts:
{facts}

Respond with a single JSON object only:
{{"llm_summary": "one sentence"}}"""

        raw = self._chat(prompt, max_tokens=256, json_mode=True)
        summary = self._strip_currency(self._parse_single_summary(raw))
        if not summary or not self._summary_is_grounded(summary, facts):
            logger.warning(
                "Discarding ungrounded LLM_SUMMARY for %s / %s. Preview: %.200s",
                row.get("ISSUE_TYPE", ""),
                row.get("ROW_IDENTIFIER", ""),
                summary or raw,
            )
            return fallback
        return summary

    def _issue_row_facts(self, row: pd.Series) -> str:
        """Build a fact list from this row's issue fields only.

        Extra result columns (other codes, amounts, etc.) are omitted so the
        model cannot treat them as additional violations.
        """
        lines: list[str] = []
        seen: set[str] = set()

        def add(col: str, val: Any) -> None:
            if col in seen or col in _META_SKIP_COLUMNS or pd.isna(val):
                return
            if isinstance(val, str) and not val.strip():
                return
            seen.add(col)
            label = _FACT_LABELS.get(col, col)
            lines.append(f"- {label}: {self._format_fact_value(col, val)}")

        for col in _FACT_COLUMN_ORDER:
            if col in row.index:
                add(col, row[col])
        return "\n".join(lines)

    @staticmethod
    def _format_fact_value(col: str, val: Any) -> str:
        """Render a row value so the model can copy it without converting."""
        if isinstance(val, pd.Timestamp):
            return str(val)
        if hasattr(val, "item"):
            try:
                val = val.item()
            except (ValueError, AttributeError):
                return str(val)
        if col == "NULL_RATE":
            try:
                rate = float(val)
            except (TypeError, ValueError):
                return str(val)
            pct = rate * 100.0 if rate <= 1.0 else rate
            return f"{rate} ({pct:.1f}%)"
        return str(val)

    @staticmethod
    def _fallback_row_summary(row: pd.Series) -> str:
        """Grounded sentence built only from identifier and ISSUE_DETAIL."""
        ident = str(row.get("ROW_IDENTIFIER") or row.get("CLIENT_ID") or "").strip()
        detail = str(row.get("ISSUE_DETAIL") or "").strip()
        if ident and ident != "TABLE_LEVEL" and detail:
            return LlamaAnalyzer._strip_currency(f"{ident}: {detail}")
        if detail:
            return LlamaAnalyzer._strip_currency(detail)
        if ident and ident != "TABLE_LEVEL":
            return f"{ident} flagged for {row.get('ISSUE_TYPE') or 'data quality issue'}"
        return str(row.get("ISSUE_TYPE") or "Data quality issue")

    def _parse_single_summary(self, raw: str) -> str:
        """Extract ``llm_summary`` from a JSON object, or empty string on failure."""
        text = raw.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return ""
        if not isinstance(parsed, dict):
            return ""
        return str(parsed.get("llm_summary") or "").strip()

    @staticmethod
    def _strip_currency(text: str) -> str:
        """Remove currency symbols and common currency codes from a summary."""
        if not text:
            return text
        cleaned = _CURRENCY_RE.sub("", text)
        return re.sub(r"\s{2,}", " ", cleaned).strip()

    def _summary_is_grounded(self, summary: str, facts: str) -> bool:
        """Return True when every code and number in the summary appears in the facts."""
        if re.search(r"\[[A-Z][A-Z0-9_]*\]", summary):
            return False
        if _ISSUE_AS_ID_RE.search(summary):
            return False
        if _CURRENCY_RE.search(summary):
            return False

        fact_upper = facts.upper()
        for code in _CODE_RE.findall(summary):
            if code.upper() not in fact_upper:
                return False

        fact_numbers = self._extract_numbers(facts)
        for number in self._extract_numbers(summary):
            if not self._number_in_facts(number, fact_numbers):
                return False
        return True

    @staticmethod
    def _extract_numbers(text: str) -> list[float]:
        """Pull numeric literals out of text, ignoring identifier codes such as C002."""
        masked = _CODE_RE.sub(" ", text)
        values: list[float] = []
        for match in _NUMBER_RE.findall(masked.replace(",", "")):
            try:
                values.append(float(match))
            except ValueError:
                continue
        return values

    @staticmethod
    def _number_in_facts(number: float, fact_numbers: list[float]) -> bool:
        """Allow an exact fact number, or a percent form of a 0–1 null rate."""
        for fact in fact_numbers:
            if abs(number - fact) <= 1e-6 * max(1.0, abs(fact)):
                return True
            if 0 < fact <= 1 and abs(number - fact * 100) <= 0.05:
                return True
            if 0 < number <= 1 and abs(fact - number * 100) <= 0.05:
                return True
        return False
