# Data Governance Pipeline

Automated data quality and governance scanning for Snowflake. The pipeline profiles tables, applies configurable business rules, optionally explains findings with a local LLM (Llama 3.2 via Ollama), and writes a timestamped Excel report that data owners and compliance teams can act on.

It is designed for financial-services client and adviser data, but the same pattern works for any Snowflake schema once you point it at your warehouse and add your own rules.

---

## Purpose

Organizations store critical client, adviser, and holdings data in Snowflake. Issues such as duplicate keys, high null rates, archived clients still showing assets under management (AUM), or missing adviser assignments are easy to miss until they affect reporting, operations, or a regulatory review.

This pipeline exists to:

- **Discover** data quality problems across a schema, not just one table at a time
- **Enforce** business and governance rules as versioned SQL in `config/rules.yaml`
- **Author ad-hoc checks** in natural language via `config/custom_rules.csv` (Ollama writes the SQL)
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
| Custom natural-language rules | `--custom-rules` turns CSV free text into Snowflake SQL via Ollama (skips YAML + profiling) |
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

**Scale checks without scaling headcount.** Adding a production business rule is a YAML + SQL change. Exploratory checks can be written in English in a CSV and run with `--custom-rules`. The same pipeline can be pointed at a test schema or production schema by changing `.env`.

Typical stakeholders: data governance, data quality, operations, compliance, and engineering teams that own Snowflake client/adviser data.

---

## How the pipeline works

```
.env + config/config.yaml
  + config/rules.yaml          (default run)
  + config/custom_rules.csv    (--custom-rules only)
                │
                ▼
         main.py  (CLI)
                │
                ▼
    DataGovernancePipeline.run()
                │
     default:          profiling + rules.yaml SQL
     --custom-rules:   LLM SQL from CSV only
                       (profiling and rules.yaml skipped)
                │
     ┌──────────┼──────────┐
     ▼          ▼          ▼
 Snowflake   Profiler   Rule engine
  connect    (nulls,     (YAML SQL, or
  + list      dup PKs)    generated SQL)
  tables
                │
                ▼
         Combine issues
                │
                ▼
     Llama 3.2 via Ollama
     - custom-rule SQL generation (when --custom-rules)
     - table-level summaries (default mode only)
     - issue-group enrichment (unless --skip-llm)
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

Profiling can be turned off with `rules.run_generic_profiling: false` in `config.yaml`. It is also skipped when you pass `--custom-rules`.

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

Passing `--custom-rules` skips this YAML stage and generic profiling entirely. See [Custom natural-language rules](#custom-natural-language-rules).

### 4. Custom natural-language rules (optional)

When `--custom-rules` is passed, `CustomRuleRunner` loads the CSV, asks Ollama for a `SELECT`, qualifies table names with `{database}.{schema}`, rebuilds the SELECT list from the rule’s include-columns, validates the SQL, and executes it through the same `RuleEngine` as YAML rules. See [Custom natural-language rules](#custom-natural-language-rules).

### 5. LLM enrichment (optional)

If you do **not** pass `--skip-llm`, the pipeline:

1. Verifies Ollama is running and the configured model is available
2. Writes a 2–3 sentence quality summary per table that had profiling issues
3. Groups all issues by type / rule / table and asks the model for:
   - `LLM_SUMMARY`
   - `LLM_BUSINESS_IMPACT`
   - `LLM_RECOMMENDED_FIX`
   - `LLM_GOVERNANCE_NOTE`

Use `--skip-llm` to skip table summaries and issue-group enrichment. Custom-rule SQL generation still calls Ollama when `--custom-rules` is set.

### 6. Excel export

A file is written to `output/dq_governance_report_YYYYMMDD_HHMMSS.xlsx` with sheets:

| Sheet | Contents |
|---|---|
| **Overview** | Total issues, tables scanned, critical/high counts, generation timestamp |
| **Rule_Summary** | Issue counts by type, rule name, and severity |
| **Table_Profiles** | Row count, primary keys, and issue counts per table (empty in `--custom-rules` mode) |
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
│   ├── rules.yaml          # Business rules (SQL)
│   └── custom_rules.csv    # Natural-language rules for --custom-rules
├── src/
│   ├── pipeline.py         # Orchestration
│   ├── snowflake_client.py # Connection and metadata queries
│   ├── profiler.py         # Null rates and duplicate PKs
│   ├── rule_engine.py      # YAML SQL rules
│   ├── custom_rules.py     # CSV → LLM SQL → Snowflake
│   ├── llm_analyzer.py     # Ollama / Llama 3.2
│   ├── excel_exporter.py   # Multi-sheet workbook
│   ├── config_loader.py    # YAML + .env merge
│   ├── cli.py              # --skip-llm, --custom-rules, --log-level
│   └── logging_config.py
├── scripts/
│   └── snowflake_synthetic_data/
│       ├── DQ_TABLES_TEST_SCRIPT.sql     # Synthetic Snowflake test schema
│       ├── ADVISER_RULES_TEST_SCRIPT.sql # Extra IM/FP/RM test cases
│       └── CUSTOM_RULES_TEST_SCRIPT.sql  # Extra CLIENTS/ADVISERS rows for CSV rules
├── output/                 # Timestamped Excel reports (created at runtime)
├── logs/                   # Timestamped run logs (created at runtime)
├── .vscode/
│   ├── launch.json         # Debug configs (with / without LLM)
│   ├── settings.json       # Default conda interpreter and terminal activation
│   └── conda_startup.ps1   # Reads CONDA_ENV_NAME from .env, runs conda activate
├── environment.yml         # Conda environment
├── requirements.txt        # pip pin file
└── .env.example            # Credential template (copy to .env)
```

---

## Getting started

### Prerequisites

- Python 3.14 (see `environment.yml`) or a recent 3.x if you install from `requirements.txt`
- Access to a Snowflake account, warehouse, database, and schema
- [Ollama](https://ollama.com/) installed locally for LLM enrichment and for `--custom-rules` SQL generation
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

Fill Snowflake and Ollama values. `CONDA_ENV_NAME` is used by Cursor/VS Code to activate conda; `main.py` does not read it.

| Variable | Purpose |
|---|---|
| `CONDA_ENV_NAME` | Conda env to activate in Cursor/VS Code terminals (`data_gov_agent` by default; must match `name:` in `environment.yml`) |
| `SNOWFLAKE_ACCOUNT` | Snowflake account identifier |
| `SNOWFLAKE_USER` | User name |
| `SNOWFLAKE_PASSWORD` | Password |
| `SNOWFLAKE_WAREHOUSE` | Warehouse to run queries on |
| `SNOWFLAKE_DATABASE` | Database to scan |
| `SNOWFLAKE_SCHEMA` | Schema to scan |
| `SNOWFLAKE_ROLE` | Optional role |
| `OLLAMA_MODEL` | Default `llama3.2` |
| `OLLAMA_HOST` | Default `http://localhost:11434` |

Pull the model once if you will use LLM analysis or `--custom-rules`:

```bash
ollama pull llama3.2
```

Confirm Ollama is running (typically `ollama serve` or the desktop app).

### 3. Optional: load the synthetic test schema

To try the pipeline without production data, run in a Snowflake worksheet **in this order**:

1. `scripts/snowflake_synthetic_data/DQ_TABLES_TEST_SCRIPT.sql` — creates `DQ_TEST_DB.DQ_TEST_SCHEMA` with sample `CLIENTS`, `ADVISERS`, and related tables seeded with known YAML / profiler issues
2. `scripts/snowflake_synthetic_data/ADVISER_RULES_TEST_SCRIPT.sql` — optional; extra IM/FP/RM scenarios (updates existing seed rows)
3. `scripts/snowflake_synthetic_data/CUSTOM_RULES_TEST_SCRIPT.sql` — optional; **adds** `ADV101` / `CR*` rows for `--custom-rules` without changing C001–C015

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
python main.py --custom-rules
python main.py --custom-rules config/custom_rules.csv
python main.py --custom-rules --skip-llm
```

On success the console prints tables scanned, total issues, report path, and log path.

### 7. Debug in VS Code / Cursor

Use the launch configurations in `.vscode/launch.json`. They load `.env` and run `main.py` with the `data_gov_agent` conda interpreter.

| Configuration | What it runs |
|---|---|
| **Run main.py (no LLM)** | `python main.py --skip-llm` — Snowflake scan and Excel report only |
| **Run main.py (with LLM)** | `python main.py` — full run including Ollama |
| **Run main.py (custom rules)** | `python main.py --custom-rules` — CSV natural-language rules only (Ollama required for SQL generation) |

Select **Run main.py (no LLM)** and start debugging (F5). That is the faster path for connectivity and rule changes.

Workspace settings in `.vscode/settings.json` point the editor at the same conda env and open terminals with `.vscode/conda_startup.ps1`. That script reads `CONDA_ENV_NAME` from `.env` and runs `conda activate <name>` so the integrated terminal (and the debug terminal) use the project environment.

If your Conda install is not under `%USERPROFILE%\anaconda3`, or you renamed the env, update:

1. `CONDA_ENV_NAME` in `.env` (used by `conda_startup.ps1`)
2. The `python` path in `.vscode/launch.json`
3. `python.defaultInterpreterPath` in `.vscode/settings.json`

`debugpy` is listed in `requirements.txt` / `environment.yml` so the Python debugger can attach to this env.

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

Do not put ad-hoc natural-language checks in `rules.yaml`. Use `--custom-rules` and `config/custom_rules.csv` instead.

---

## Custom natural-language rules

`--custom-rules` is off by default. When you pass it, the pipeline **does not** run generic profiling (`duplicate_primary_key`, `high_null_rate`) or any SQL in `config/rules.yaml`. Only the CSV rules run. Results still go through the same Excel path (Row_Issues, Rule_Summary, optional `LLM_*` enrichment).

```bash
python main.py --custom-rules
python main.py --custom-rules path/to/my_rules.csv
python main.py --custom-rules --skip-llm   # still generates SQL with Ollama; skips issue enrichment
```

If you omit the path, the default file is `config/custom_rules.csv`.

| Flags | Profiling + `rules.yaml` | CSV → SQL (Ollama) | Table summaries + `LLM_*` columns |
|---|---|---|---|
| none | yes | no | yes |
| `--skip-llm` | yes | no | no |
| `--custom-rules` | no | yes | yes |
| `--custom-rules --skip-llm` | no | yes | no |

### How SQL is generated

For each enabled CSV row, `src/custom_rules.py`:

1. Loads column metadata from Snowflake for `table` plus `related_tables`
2. Builds a SELECT checklist from “include … in the result” (mapped onto real column names, e.g. “adviser name” → `ADVISER_NAME`) and a WHERE checklist from the wording (null / zero / negative / non-deleted / `COL = FALSE`)
3. Asks Ollama for a single-line `SELECT` using **unqualified** table names (JSON mode cannot reliably emit `{database}` braces)
4. Prefixes `FROM` / `JOIN` tables with `{database}.{schema}.`
5. Rebuilds the SELECT list from the checklist plus `'<id>' AS ISSUE_TYPE` and `ISSUE_DETAIL`
6. Validates safety (SELECT/WITH only) and checklist coverage; retries up to 3 times
7. Executes through `RuleEngine` (same `RULE_ERROR` behaviour as YAML rules)

Generated SQL is written to the run log under `logs/`.

Do not put custom rules in `rules.yaml`. Promote a stable check into YAML only after you have reviewed the SQL.

### CSV template

| Column | Required | Purpose |
|---|---|---|
| `id` | yes | Becomes `ISSUE_TYPE` |
| `name` | yes | Shown as `RULE_NAME` in the report |
| `table` | yes | Primary table; must exist in the target schema |
| `primary_key` | recommended | Comma-separated columns used for `ROW_IDENTIFIER` |
| `severity` | no | `critical` / `high` / `medium` / `low` (default `medium`) |
| `enabled` | no | `true` / `false` (default `true`) |
| `related_tables` | no | Extra tables whose columns are given to the LLM (needed for joins) |
| `rule_text` | yes | Natural-language instruction; quote the field if it contains commas |

Shipped example (`config/custom_rules.csv`):

```csv
id,name,table,primary_key,severity,enabled,related_tables,rule_text
inactive_im_assigned,Non-deleted clients assigned to an inactive IM,CLIENTS,CLIENT_ID,high,TRUE,ADVISERS,"Flag every non-deleted client whose IM_CODE matches an adviser with IS_ACTIVE = FALSE. Include CLIENT_ID, IM_CODE, adviser name, CLIENT_STATUS and IS_ACTIVE in the result."
zero_or_negative_aum_active,Active clients with zero or negative AUM,CLIENTS,CLIENT_ID,medium,TRUE,,"Flag every client with CLIENT_STATUS is ACTIVE, IS_DELETED = FALSE, and AUM that is null, zero, or negative. Include CLIENT_ID, CLIENT_STATUS, and AUM."
```

After `CUSTOM_RULES_TEST_SCRIPT.sql`, those two rules should flag:

| Rule | Expected `CLIENT_ID` |
|---|---|
| `inactive_im_assigned` | C014 (seed, inactive `IM003`), CR001 (new, inactive `IM101`) |
| `zero_or_negative_aum_active` | CR011 (AUM 0), CR012 (negative AUM), CR013 (NULL AUM) |

Controls that must not fire: CR003 (deleted), CR004 (active IM), CR014 (positive AUM), CR015 (archived), CR016 (suspended), CR017 (deleted).

Write `rule_text` so every alternative you care about is explicit (for example “null, zero, or negative”). A comparison such as `AUM <= 0` does not match NULL; the generator is instructed to OR in `IS NULL` when the text mentions null.

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
| `--custom-rules` fails before Snowflake queries | Ollama must be running; SQL generation is not skipped by `--skip-llm` |
| `Custom rules file not found` | Pass a real path or keep `config/custom_rules.csv` |
| Custom rule appears as `RULE_ERROR` | Check the log for `candidate SQL` / `generated SQL`. Table/column names must exist; `related_tables` is required for joins |
| Custom rule returns fewer rows than expected | Confirm `CUSTOM_RULES_TEST_SCRIPT.sql` has been applied; C001–C015 alone are not enough for `zero_or_negative_aum_active` |
| Rule appears as `RULE_ERROR` | YAML SQL failed (wrong table/column names). Fix the rule; other rules still ran |
| Empty report / no tables | Schema name, privileges on `INFORMATION_SCHEMA`, and `exclude_tables` |
| Slow runs | Large tables: profiling issues one COUNT per column. Use `--skip-llm` or disable profiling while iterating on YAML rules. `--custom-rules` is slower because each rule calls Ollama |
| Debugger uses the wrong Python / `ModuleNotFoundError` | Select interpreter `data_gov_agent`, or confirm `.vscode/launch.json` `python` path |
| `No module named 'debugpy'` | `pip install debugpy` in the conda env, or recreate with `environment.yml` |
| `conda activate` fails in the terminal | Set `CONDA_ENV_NAME` in `.env` to an env from `conda env list`; start Cursor from an Anaconda Prompt if conda is not on PATH |
| Debugpy frozen-modules warning | Harmless. Debugging still proceeds |

---

## License

Use and extend this project according to your organization’s internal policies. Add a `LICENSE` file if you publish the repository publicly.
