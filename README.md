# Data Governance Pipeline

Automated data quality and governance scanning for Snowflake. The pipeline profiles tables, applies configurable business rules, optionally explains findings with a local LLM (Llama 3.2 via Ollama), and writes a timestamped Excel report that data owners and compliance teams can act on.

It is designed for financial-services client and adviser data, but the same pattern works for any Snowflake schema once you point it at your warehouse and add your own rules.

---

## Purpose

Organizations store critical client, adviser, and holdings data in Snowflake. Issues such as duplicate keys, high null rates, archived clients still showing assets under management (AUM), or missing adviser assignments are easy to miss until they affect reporting, operations, or a regulatory review.

This pipeline exists to:

- **Discover** data quality problems across a schema, not just one table at a time
- **Enforce** business and governance rules as versioned SQL in `config/rules.yaml`
- **Explain** findings in plain language so non-engineers can understand impact and next steps
- **Deliver** a repeatable Excel report for stewards, operations, and audit

Data never leaves your environment for LLM analysis: Ollama runs locally. Snowflake credentials stay in a local `.env` file that is not committed to git.

---

## What it helps you do

| Capability | What you get |
|---|---|
| Schema-wide scan | Lists base tables in the configured Snowflake database/schema and profiles each one |
| Generic quality checks | Flags columns above a null-rate threshold and duplicate primary-key groups |
| Business rule checks | Runs SQL rules (for example: archived clients with AUM, invalid IM/FP/RM) |
| Local LLM enrichment | Adds summary, business impact, recommended fix, and governance notes per issue group |
| Excel governance pack | Overview metrics, rule counts, table profiles, row-level issues, and table-level LLM summaries |
| Configurable scope | Toggle profiling vs business rules, exclude tables, override primary keys, tune thresholds |

---

## Business value

**Reduce operational and regulatory risk.** Stale or incorrect client records (archived clients still showing AUM, legacy records that should have been deleted, missing relationship managers) create reporting errors and audit findings. Catching them in a scheduled scan is cheaper than discovering them in a client review or regulator request.

**Give data owners a single source of truth.** The Excel report consolidates technical issues (nulls, duplicate keys) and policy issues (adviser assignment, status vs AUM) into one artifact. Stewards can prioritize by severity instead of piecing together ad-hoc queries.

**Shorten time from finding to fix.** LLM columns explain *why* an issue matters and *what to do*, so analysts and operations teams spend less time translating SQL results into action.

**Keep sensitive data in-house.** Client identifiers and sample rows are analyzed with a local model. There is no cloud LLM API and no need to send production data to a third party.

**Make governance repeatable.** Rules live in YAML, credentials in environment variables, and each run produces a dated report plus a log file. That supports change control, re-runs after remediations, and evidence for data-governance programs.

**Scale checks without scaling headcount.** Adding a new business rule is a YAML + SQL change, not a new one-off worksheet. The same pipeline can be pointed at a test schema or production schema by changing `.env`.

Typical stakeholders: data governance, data quality, operations, compliance, and engineering teams that own Snowflake client/adviser data.

---

## How the pipeline works

```
.env + config/config.yaml + config/rules.yaml
                │
                ▼
         main.py  (CLI)
                │
                ▼
    DataGovernancePipeline.run()
                │
     ┌──────────┼──────────┐
     ▼          ▼          ▼
 Snowflake   Profiler   Rule engine
  connect    (nulls,     (SQL rules
  + list      dup PKs)    from YAML)
  tables
                │
                ▼
         Combine issues
                │
                ▼
     Llama 3.2 via Ollama  (optional)
     - table-level summaries
     - issue-group enrichment
                │
                ▼
     Excel report in output/
     Log file in logs/
```

### 1. Connect and list tables

`SnowflakeClient` authenticates with credentials from `.env`, then lists base tables in `SNOWFLAKE_DATABASE` / `SNOWFLAKE_SCHEMA`. Names in `config/config.yaml` `exclude_tables` are skipped.

Primary keys are resolved in this order:

1. Manual override in `config.yaml` (`table_primary_keys`)
2. Snowflake `SHOW PRIMARY KEYS IN TABLE`
3. Heuristic (non-nullable column ending in `ID` or `KEY`, else the first column)

### 2. Generic profiling

For each table, `TableProfiler` measures:

- Per-column null count, null rate, and distinct count
- Duplicate primary-key groups (`HAVING COUNT(*) > 1`)

Columns whose null rate exceeds `profiling.null_rate_threshold` (default 5%) become `high_null_rate` issues. Duplicate keys become `duplicate_primary_key` issues with severity **critical**.

Profiling can be turned off with `rules.run_generic_profiling: false` in `config.yaml`.

### 3. Business rules

`RuleEngine` runs every **enabled**, non-dynamic rule in `config/rules.yaml`. Each rule is a SQL template with `{database}` and `{schema}` placeholders. The query must return:

- Identifier columns (typically the primary key)
- `ISSUE_TYPE` — rule id
- `ISSUE_DETAIL` — human-readable violation

If a rule’s SQL fails, the pipeline records a `RULE_ERROR` row instead of aborting the whole run.

Shipped example rules (wealth / client data):

| Rule id | Severity | Intent |
|---|---|---|
| `archived_client_with_aum` | high | Archived clients should not still show AUM |
| `pre_2017_client_not_deleted` | high | Legacy clients created before 2017 should be marked deleted |
| `invalid_im_fp` | medium | Each active client needs a valid IM and/or FP adviser code |
| `invalid_rm` | medium | Each active client needs a valid relationship manager |

Dynamic rules (`duplicate_primary_key`, `high_null_rate`) are metadata only; they are produced by the profiler, not executed as SQL.

Business rules can be turned off with `rules.run_business_rules: false`.

### 4. LLM enrichment (optional)

If you do **not** pass `--skip-llm`, the pipeline:

1. Verifies Ollama is running and the configured model is available
2. Writes a 2–3 sentence quality summary per table that had profiling issues
3. Groups all issues by type / rule / table and asks the model for:
   - `LLM_SUMMARY`
   - `LLM_BUSINESS_IMPACT`
   - `LLM_RECOMMENDED_FIX`
   - `LLM_GOVERNANCE_NOTE`

Use `--skip-llm` to test Snowflake connectivity or to produce a faster, rules-only report.

### 5. Excel export

A file is written to `output/dq_governance_report_YYYYMMDD_HHMMSS.xlsx` with sheets:

| Sheet | Contents |
|---|---|
| **Overview** | Total issues, tables scanned, critical/high counts, generation timestamp |
| **Rule_Summary** | Issue counts by type, rule name, and severity |
| **Table_Profiles** | Row count, primary keys, and issue counts per table |
| **Row_Issues** | Every flagged row or table-level issue, plus LLM columns when enabled |
| **LLM_Table_Summaries** | Narrative quality summary per table |

Logs go to `logs/log_YYYYMMDD_HHMMSS.txt`.

---

## Project layout

```
data_governance_pipeline/
├── main.py                 # Entry point
├── config/
│   ├── config.yaml         # Thresholds, output, which rule categories to run
│   └── rules.yaml          # Business rules (SQL)
├── src/
│   ├── pipeline.py         # Orchestration
│   ├── snowflake_client.py # Connection and metadata queries
│   ├── profiler.py         # Null rates and duplicate PKs
│   ├── rule_engine.py      # YAML SQL rules
│   ├── llm_analyzer.py     # Ollama / Llama 3.2
│   ├── excel_exporter.py   # Multi-sheet workbook
│   ├── config_loader.py    # YAML + .env merge
│   ├── cli.py              # --skip-llm, --log-level
│   └── logging_config.py
├── scripts/
│   ├── run_local.ps1                 # Windows helper
│   ├── DQ_TABLES_TEST_SCRIPT.sql     # Synthetic Snowflake test schema
│   └── ADVISER_RULES_TEST_SCRIPT.sql # Extra IM/FP/RM test cases
├── environment.yml         # Conda environment
├── requirements.txt        # pip pin file
└── .env.example            # Credential template (copy to .env)
```

---

## Getting started

### Prerequisites

- Python 3.14 (see `environment.yml`) or a recent 3.x if you install from `requirements.txt`
- Access to a Snowflake account, warehouse, database, and schema
- [Ollama](https://ollama.com/) installed locally if you want LLM enrichment
- Conda (recommended) or pip

### 1. Clone and create the environment

```bash
git clone <repository-url>
cd data_governance_pipeline
```

**Conda (recommended):**

```bash
conda env create -f environment.yml
conda activate data_gov_agent
```

**pip:**

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Snowflake and Ollama

Copy the example env file and fill in your values. Do not commit `.env`.

```bash
copy .env.example .env
```

Required variables:

| Variable | Purpose |
|---|---|
| `SNOWFLAKE_ACCOUNT` | Snowflake account identifier |
| `SNOWFLAKE_USER` | User name |
| `SNOWFLAKE_PASSWORD` | Password |
| `SNOWFLAKE_WAREHOUSE` | Warehouse to run queries on |
| `SNOWFLAKE_DATABASE` | Database to scan |
| `SNOWFLAKE_SCHEMA` | Schema to scan |
| `SNOWFLAKE_ROLE` | Optional role |
| `OLLAMA_MODEL` | Default `llama3.2` |
| `OLLAMA_HOST` | Default `http://localhost:11434` |

Pull the model once if you will use LLM analysis:

```bash
ollama pull llama3.2
```

Confirm Ollama is running (typically `ollama serve` or the desktop app).

### 3. Optional: load the synthetic test schema

To try the pipeline without production data, run in a Snowflake worksheet:

1. `scripts/DQ_TABLES_TEST_SCRIPT.sql` — creates `DQ_TEST_DB.DQ_TEST_SCHEMA` with sample `CLIENTS`, `ADVISERS`, and related tables seeded with known issues
2. `scripts/ADVISER_RULES_TEST_SCRIPT.sql` — extra IM/FP/RM scenarios

Then set `.env` to:

```
SNOWFLAKE_DATABASE=DQ_TEST_DB
SNOWFLAKE_SCHEMA=DQ_TEST_SCHEMA
```

### 4. Tune `config/config.yaml`

- `exclude_tables` — skip system or irrelevant tables
- `table_primary_keys` — override PK detection when Snowflake has no constraint
- `profiling.null_rate_threshold` — default `0.05` (5%)
- `rules.run_generic_profiling` / `rules.run_business_rules` — enable or disable each stage
- `output.directory` / `output.filename_prefix` — report location and name prefix
- `llm.temperature` / `llm.max_tokens` — generation settings

### 5. Align `config/rules.yaml` with your schema

The sample rules assume tables named `CLIENTS` and `ADVISERS` and columns such as `CLIENT_STATUS`, `AUM`, `IM_CODE`, `FP_CODE`, and `RM_CODE`. Edit table and column names to match your warehouse, or disable rules until they are configured (`enabled: false`).

### 6. Run the pipeline

From the project root, with the conda/venv active:

```bash
python main.py
```

Useful flags:

```bash
python main.py --skip-llm
python main.py --log-level DEBUG
python main.py --skip-llm --log-level WARNING
```

On Windows you can also use:

```powershell
.\scripts\run_local.ps1
.\scripts\run_local.ps1 -SkipLlm
.\scripts\run_local.ps1 -LogLevel DEBUG
```

On success the console prints tables scanned, total issues, report path, and log path.

---

## Adding or changing business rules

Rules are data, not code. Open `config/rules.yaml` and add an entry:

```yaml
  - id: my_new_rule
    name: "Short name shown in the report"
    description: "Why this check exists"
    enabled: true
    severity: high          # critical | high | medium | low
    table: CLIENTS
    primary_key:
      - CLIENT_ID
    sql: |
      SELECT
          c.CLIENT_ID,
          'my_new_rule' AS ISSUE_TYPE,
          'What is wrong on this row' AS ISSUE_DETAIL
      FROM {database}.{schema}.CLIENTS c
      WHERE <condition>
```

Requirements:

- SQL **must** select `ISSUE_TYPE` and `ISSUE_DETAIL`
- Use `{database}` and `{schema}` so the same rule works across environments
- Include primary-key columns so `ROW_IDENTIFIER` in Excel is useful
- Set `enabled: false` to keep a rule in source control without running it
- Set `dynamic: true` only for profiler-backed metadata rules (no SQL)

After saving, re-run `python main.py`. New issues appear on **Row_Issues** and roll up on **Rule_Summary**.

---

## Reading the report

Start with **Overview** for volume and severity, then **Rule_Summary** to see which rules fire most. Open **Row_Issues** to remediate specific keys. Use **LLM_Table_Summaries** and the `LLM_*` columns when you need a narrative for a governance pack or ticket.

Severity guide:

- **critical** — identity integrity (duplicate primary keys)
- **high** — policy or status conflicts that can misstate AUM or retain records that should be gone
- **medium** — incomplete or invalid reference data (adviser / RM assignment)
- **low** — completeness signals (high null rates) that may be expected on optional columns

---

## Security notes

- `.env` is gitignored. Never commit passwords or live account details.
- `.env.example` is a template only; replace placeholders with your own account.
- The pipeline uses warehouse compute. Prefer a dedicated or non-peak warehouse for large schemas.
- LLM prompts include sample issue rows. Keep Ollama on a machine that is allowed to see that data.

---

## Troubleshooting

| Symptom | What to check |
|---|---|
| `SNOWFLAKE_DATABASE and SNOWFLAKE_SCHEMA must be set` | Fill both in `.env` |
| `SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER must be set` | Fill account and user in `.env` |
| Snowflake auth / warehouse errors | Account identifier, password, role, and that the warehouse is running |
| `Model 'llama3.2' not found in Ollama` | `ollama pull llama3.2` and confirm `OLLAMA_HOST` |
| Rule appears as `RULE_ERROR` | SQL failed (wrong table/column names). Fix the rule; other rules still ran |
| Empty report / no tables | Schema name, privileges on `INFORMATION_SCHEMA`, and `exclude_tables` |
| Slow runs | Large tables: profiling issues one COUNT per column. Use `--skip-llm` or disable profiling while iterating on rules |

---

## License

Use and extend this project according to your organization’s internal policies. Add a `LICENSE` file if you publish the repository publicly.
