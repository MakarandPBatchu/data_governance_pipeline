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
    """Enrich data-quality findings with narrative analysis from a local Ollama model."""

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
    def _chat(self, prompt: str, _max_tokens: int | None = 1024) -> str:
        """Send a prompt to Ollama and return the model's text response.

        Retries up to 3 times with exponential backoff on transient failures.

        Args:
            prompt: User message content.
            _max_tokens: Maximum number of tokens to generate.
        Returns:
            Stripped text content from the model response.
        """
        response = self._client.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            options={
                "temperature": self.temperature,
                "num_predict": max(self.max_tokens, _max_tokens),
            },
        )
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
            Dict with keys: summary, business_impact, recommended_fix, governance_note.
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
Use exactly these keys:
{{
  "llm_summary": "One sentence describing the issue",
  "business_impact": "What business risk this creates",
  "recommended_fix": "Concrete steps to remediate",
  "governance_note": "Any compliance or governance consideration"
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

    def enrich_issues(self, issues_df: pd.DataFrame) -> pd.DataFrame:
        """Add LLM narrative columns to every issue group in the DataFrame.

        Groups issues by ISSUE_TYPE, RULE_NAME, and TABLE_NAME, then calls
        ``analyze_issue_group`` once per group and applies the result to all
        rows in that group.

        Args:
            issues_df: Combined issue DataFrame from profiling and business rules.

        Returns:
            Copy of ``issues_df`` with LLM_SUMMARY, LLM_BUSINESS_IMPACT,
            LLM_RECOMMENDED_FIX, and LLM_GOVERNANCE_NOTE columns added.
        """
        if issues_df.empty:
            return issues_df

        enriched = issues_df.copy()
        enriched["LLM_SUMMARY"] = ""
        enriched["LLM_BUSINESS_IMPACT"] = ""
        enriched["LLM_RECOMMENDED_FIX"] = ""
        enriched["LLM_GOVERNANCE_NOTE"] = ""
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
            enriched.loc[mask, "LLM_BUSINESS_IMPACT"] = analysis.get("business_impact", "")
            enriched.loc[mask, "LLM_RECOMMENDED_FIX"] = analysis.get("recommended_fix", "")
            enriched.loc[mask, "LLM_GOVERNANCE_NOTE"] = analysis.get("governance_note", "")

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
            issue_type: Issue type (used in fallback governance note).
            rule_name: Rule name (used in fallback governance note).

        Returns:
            Dict with llm_summary, business_impact, recommended_fix, governance_note keys.
        """
        text = self._extract_json_text(raw)

        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise json.JSONDecodeError("Expected JSON object", text, 0)

            return {
                "llm_summary": str(parsed.get("llm_summary", "")),
                "business_impact": str(parsed.get("business_impact", "")),
                "recommended_fix": str(parsed.get("recommended_fix", "")),
                "governance_note": str(parsed.get("governance_note", "")),
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
                "business_impact": "",
                "recommended_fix": "Review issue manually; LLM response was not structured JSON.",
                "governance_note": f"Issue: {issue_type} / {rule_name}",
            }
