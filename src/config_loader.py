"""Load configuration from YAML files and environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a dictionary.

    Args:
        path: Path to the YAML file.

    Returns:
        Parsed YAML content, or an empty dict if the file is empty.
    """
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_settings() -> dict[str, Any]:
    """Load pipeline settings from ``.env``, ``config.yaml``, and ``rules.yaml``.

    Environment variables take precedence over YAML defaults for Snowflake
    credentials and Ollama connection settings.

    Returns:
        Nested settings dict used by the pipeline and its components.
    """
    load_dotenv(PROJECT_ROOT / ".env")

    config = load_yaml(CONFIG_DIR / "config.yaml")
    rules = load_yaml(CONFIG_DIR / "rules.yaml")

    settings: dict[str, Any] = {
        "project_root": PROJECT_ROOT,
        "config": config,
        "rules": rules.get("rules", []),
        "snowflake": {
            "account": os.getenv("SNOWFLAKE_ACCOUNT", ""),
            "user": os.getenv("SNOWFLAKE_USER", ""),
            "password": os.getenv("SNOWFLAKE_PASSWORD", ""),
            "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE"),
            "database": os.getenv("SNOWFLAKE_DATABASE") ,
            "schema": os.getenv("SNOWFLAKE_SCHEMA") ,
            "role": os.getenv("SNOWFLAKE_ROLE"),
        },
        "profiling": config.get("profiling", {}),
        "output": config.get("output", {}),
        "llm": {
            "model": os.getenv("OLLAMA_MODEL"),
            "host": os.getenv("OLLAMA_HOST"),
            "temperature": config.get("llm", {}).get("temperature", 0.1),
            "max_tokens": config.get("llm", {}).get("max_tokens", 1024),
        },
        "rule_flags": config.get("rules", {}),
        "exclude_tables": config.get("exclude_tables", [])
    }
    return settings
