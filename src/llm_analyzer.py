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
        self.sample_size = settings.get("profiling", {}).get("sample_size_for_llm", 5)
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
        _max_tokens: int | None = 1024,
        *,
        json_mode: bool = False,
    ) -> str:
        """Send a prompt to Ollama and return the model's text response.

        Retries up to 3 times with exponential backoff on transient failures.

        Args:
            prompt: User message content.
            _max_tokens: Maximum number of tokens to generate.
            json_mode: When True, ask Ollama to emit a JSON object.
        Returns:
            Stripped text content from the model response.
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "options": {
                "temperature": self.temperature,
                "num_predict": max(self.max_tokens, _max_tokens),
            },
        }
        if json_mode:
            kwargs["format"] = "json"
        response = self._client.chat(**kwargs)
        return response.message.content.strip()

    def analyze_issue_group(
        self,
        issue_type: str,
        rule_name: str,
        severity: str,
        rows: pd.DataFrame,
        table_name: str,
    ) -> dict[str, str]:
        """Ask the LLM to analyse one group of related issues.

        Args:
            issue_type: Issue category identifier (e.g. ``archived_client_with_aum``).
            rule_name: Human-readable rule name for the prompt.
            severity: Issue severity level (critical, high, medium, low).
            rows: Sample of affected rows per issue type group
            table_name: Snowflake table where the issue was found.

        Returns:
            Dict with key ``llm_summary``.
        """
        sample_json = rows.head(self.sample_size).to_dict(orient="records")
        prompt = f"""You are a data governance analyst for a financial services firm.
Analyze the following data quality issue found in Snowflake.

Table: {table_name}
Issue type: {issue_type}
Rule: {rule_name}
Severity: {severity}
Sample affected rows (JSON):
{json.dumps(sample_json, indent=2, default=str)}

Respond with a single JSON object only. No markdown, no code fences, no text before or after.
Use exactly this key:
{{
  "llm_summary": "One sentence describing the issue"
}}"""

        raw = self._chat(prompt)
        return self._parse_llm_json(raw, issue_type, rule_name)

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

        raw = self._chat(prompt, _max_tokens=2048, json_mode=True)
        return self._parse_sql_response(raw)

    def _parse_sql_response(self, raw: str) -> str:
        """Extract a SQL string from JSON or a fenced SQL block."""
        for candidate in (raw.strip(),):
            try:
                parsed = json.loads(candidate)
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
        """Add an LLM summary column to every issue group in the DataFrame.

        Groups issues by ISSUE_TYPE, RULE_NAME, and TABLE_NAME, then calls
        ``analyze_issue_group`` once per group and applies the result to all
        rows in that group.

        Args:
            issues_df: Combined issue DataFrame from profiling and business rules.

        Returns:
            Copy of ``issues_df`` with an ``LLM_SUMMARY`` column added.
        """
        if issues_df.empty:
            return issues_df

        enriched = issues_df.copy()
        enriched["LLM_SUMMARY"] = ""
        avg_time_per_issue_group: list[float] = []

        group_cols = ["ISSUE_TYPE", "RULE_NAME", "TABLE_NAME"]
        existing = [c for c in group_cols if c in enriched.columns]

        
        for keys, group in enriched.groupby(existing, dropna=False):
            
            if isinstance(keys, tuple):
                issue_type, rule_name, table_name = keys
            else:
                issue_type, rule_name, table_name = keys, "", ""

            logger.info(f"Analyzing issue group - {keys}")

            start_time = time.time()
            analysis = self.analyze_issue_group(
                issue_type=str(issue_type),
                rule_name=str(rule_name or issue_type),
                severity=str(group["SEVERITY"].iloc[0]) if "SEVERITY" in group.columns else "medium",
                rows=group,
                table_name=str(table_name),
            )
            end_time = time.time()
            avg_time_per_issue_group.append(end_time - start_time)

            mask = True
            for col, val in zip(existing, keys if isinstance(keys, tuple) else (keys,)):
                mask = mask & (enriched[col] == val)

            enriched.loc[mask, "LLM_SUMMARY"] = analysis.get("llm_summary", "")

        if avg_time_per_issue_group:
            logger.info(f"Average time taken by LLM per issue group: {sum(avg_time_per_issue_group) / len(avg_time_per_issue_group)} seconds")
        return enriched

    def _extract_json_text(self, raw: str) -> str:
        """Pull a JSON object string out of a free-form LLM response."""
        text = raw.strip()

        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
        if fence_match:
            text = fence_match.group(1).strip()

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

        return text.strip()

    def _parse_llm_json(self, raw: str, issue_type: str, rule_name: str) -> dict[str, str]:
        """Parse the LLM's JSON response, falling back gracefully on malformed output.

        Handles markdown fences, leading/trailing prose, and minor formatting issues
        common in local model output.

        Args:
            raw: Raw text returned by the LLM.
            issue_type: Issue type (used in the parse-failure warning).
            rule_name: Rule name (used in the parse-failure warning).

        Returns:
            Dict with an ``llm_summary`` key.
        """
        text = self._extract_json_text(raw)

        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise json.JSONDecodeError("Expected JSON object", text, 0)

            return {
                "llm_summary": str(parsed.get("llm_summary", "")),
            }
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "LLM returned non-JSON response for %s / %s: %s. Raw preview: %.200s",
                issue_type,
                rule_name,
                exc,
                raw,
            )
            return {
                "llm_summary": raw[:500],
            }
