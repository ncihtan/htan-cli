"""Query HTAN metadata in ISB-CGC BigQuery.

Supports direct SQL, table listing, and schema inspection.

Usage as library::

    from htan.query.bq import BigQueryClient
    client = BigQueryClient()
    tables = client.list_tables()
    schema = client.describe_table("clinical_tier1_demographics")

Usage as CLI::

    htan query bq tables
    htan query bq describe clinical_tier1_demographics
    htan query bq sql "SELECT COUNT(*) FROM ..."
"""

import csv
import io
import json
import re
import sys

import click


HTAN_DATASET = "isb-cgc-bq.HTAN"
HTAN_DATASET_VERSIONED = "isb-cgc-bq.HTAN_versioned"
DEFAULT_LIMIT = 1000

BLOCKED_SQL_KEYWORDS = [
    "DELETE", "DROP", "UPDATE", "INSERT", "CREATE",
    "ALTER", "TRUNCATE", "MERGE", "GRANT", "REVOKE",
]
ALLOWED_SQL_STARTS = ["SELECT", "WITH", "SHOW", "EXPLAIN"]
TABLE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")

# Key table schemas for natural language query context
TABLE_SCHEMAS_SUMMARY = """
=== HTAN BigQuery Table Schemas (isb-cgc-bq.HTAN) ===
Tables use _current suffix, which always points to the latest release.
For reproducible analyses with a specific version, use isb-cgc-bq.HTAN_versioned with _rN suffixes.

--- Clinical tables ---

Table: clinical_tier1_demographics_current
  HTAN_Participant_ID (STRING) - Participant identifier, e.g. HTA1_1001
  HTAN_Center (STRING) - Atlas center, e.g. 'HTAN HTAPP', 'HTAN HMS'
  Age_at_Diagnosis (INTEGER) - Age in days at diagnosis
  Gender (STRING) - male, female
  Race (STRING) - e.g. white, black or african american, asian
  Ethnicity (STRING) - e.g. not hispanic or latino, hispanic or latino
  Vital_Status (STRING) - Alive, Dead

Table: clinical_tier1_diagnosis_current
  HTAN_Participant_ID (STRING)
  HTAN_Center (STRING)
  Primary_Diagnosis (STRING) - ICD-O-3 diagnosis
  Site_of_Resection_or_Biopsy (STRING)
  Tissue_or_Organ_of_Origin (STRING)
  Tumor_Grade (STRING) - G1, G2, G3
  AJCC_Pathologic_Stage (STRING)
  Morphology (STRING)

--- Biospecimen ---

Table: biospecimen_current
  HTAN_Biospecimen_ID (STRING) - e.g. HTA1_1001_001
  HTAN_Participant_ID (STRING)
  HTAN_Center (STRING)
  Biospecimen_Type (STRING)
  Preservation_Method (STRING)
  Tumor_Tissue_Type (STRING)

--- Assay metadata ---

Table: scRNAseq_level1_metadata_current (also level2, level3, level4)
  HTAN_Parent_Biospecimen_ID (STRING)
  HTAN_Data_File_ID (STRING)
  Library_Construction_Method (STRING)
  Filename (STRING)
  File_Size (INTEGER) -- file size in bytes (present in ALL assay metadata tables)
  entityId (STRING) -- Synapse ID (present in ALL assay metadata tables)
  HTAN_Center (STRING)

=== Notes ===
- Join clinical tables on HTAN_Participant_ID
- Join assay to biospecimen on HTAN_Parent_Biospecimen_ID = HTAN_Biospecimen_ID
- Dataset: isb-cgc-bq.HTAN (use fully qualified table names with _current suffix)
- File_Size (INTEGER, bytes) and entityId (STRING, Synapse ID) exist in ALL assay metadata tables
""".strip()


class BigQueryError(Exception):
    """BigQuery operation error."""
    pass


def validate_sql_safety(sql):
    """Validate that SQL is read-only. Returns (safe, reason)."""
    normalized = " ".join(sql.upper().split())
    for keyword in BLOCKED_SQL_KEYWORDS:
        pattern = r"\b" + keyword + r"\b"
        if re.search(pattern, normalized):
            return False, f"Blocked SQL keyword: {keyword}"
    first_word = normalized.split()[0] if normalized.split() else ""
    if first_word not in ALLOWED_SQL_STARTS:
        return False, f"SQL must start with one of: {', '.join(ALLOWED_SQL_STARTS)}"
    return True, "OK"


def _ensure_limit(sql, limit=DEFAULT_LIMIT):
    normalized = " ".join(sql.upper().split())
    if "LIMIT" not in normalized:
        sql = sql.rstrip().rstrip(";")
        sql += f"\nLIMIT {limit}"
        print(f"Auto-applied LIMIT {limit}", file=sys.stderr)
    return sql


def _get_bq_module():
    try:
        from google.cloud import bigquery
        return bigquery
    except ImportError:
        raise BigQueryError(
            "google-cloud-bigquery not installed. Run: pip install htan[bigquery]"
        )


class BigQueryClient:
    """High-level client for HTAN BigQuery queries.

    Requires: pip install htan[bigquery]

    Usage:
        client = BigQueryClient()
        tables = client.list_tables()
    """

    def __init__(self, project=None):
        """Initialize BigQuery client.

        Args:
            project: Google Cloud project ID for billing. If None, uses default.
        """
        import os
        bq = _get_bq_module()
        project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        try:
            self._client = bq.Client(project=project) if project else bq.Client()
        except Exception as e:
            raise BigQueryError(
                f"Could not create BigQuery client: {e}\n"
                "Run 'gcloud auth application-default login' or set GOOGLE_APPLICATION_CREDENTIALS"
            )

    def query(self, sql, limit=DEFAULT_LIMIT, dry_run=False):
        """Execute a read-only SQL query. Returns pandas DataFrame."""
        bq = _get_bq_module()
        safe, reason = validate_sql_safety(sql)
        if not safe:
            raise BigQueryError(reason)
        sql = _ensure_limit(sql, limit)

        if dry_run:
            job_config = bq.QueryJobConfig(dry_run=True, use_query_cache=False)
            job = self._client.query(sql, job_config=job_config)
            return {"bytes_processed": job.total_bytes_processed or 0, "sql": sql}

        return self._client.query(sql).to_dataframe()

    def list_tables(self, versioned=False):
        """List available HTAN tables. Returns list of table name strings."""
        dataset = HTAN_DATASET_VERSIONED if versioned else HTAN_DATASET
        sql = f"SELECT table_name FROM `{dataset}.INFORMATION_SCHEMA.TABLES` ORDER BY table_name"
        df = self._client.query(sql).to_dataframe()
        return df["table_name"].tolist()

    def describe_table(self, table, versioned=False):
        """Describe table schema. Returns dict with table info and schema."""
        if not TABLE_NAME_PATTERN.match(table):
            raise BigQueryError(f"Invalid table name '{table}'.")
        dataset = HTAN_DATASET_VERSIONED if versioned else HTAN_DATASET
        if not versioned and not re.search(r"_(current|r\d+(_v\d+)?)$", table):
            table = f"{table}_current"
        full_table = f"{dataset}.{table}"
        try:
            tbl = self._client.get_table(full_table)
        except Exception as e:
            raise BigQueryError(f"Could not access table '{full_table}': {e}")
        return {
            "table": full_table,
            "num_rows": tbl.num_rows,
            "num_bytes": tbl.num_bytes,
            "description": tbl.description,
            "schema": [
                {"name": f.name, "type": f.field_type, "mode": f.mode, "description": f.description or ""}
                for f in tbl.schema
            ],
        }


# --- CLI ---

_BQ_EPILOG = """\
Examples:

  htan query bq tables
  htan query bq describe clinical_tier1_demographics
  htan query bq sql "SELECT COUNT(*) FROM `isb-cgc-bq.HTAN.clinical_tier1_demographics_current`"
  htan query bq query "How many breast cancer patients?"
"""


@click.group(name="bq", epilog=_BQ_EPILOG)
def bq():
    """Query HTAN metadata in ISB-CGC BigQuery."""


@bq.command(name="query")
@click.argument("question")
@click.option("--project", "-p", help="Google Cloud project ID")
@click.option("--format", "-f", "fmt", type=click.Choice(["text", "json", "csv"]), default="text")
def query_cmd(question, project, fmt):
    """Output natural-language query context for an LLM (does not execute)."""
    click.echo("=== HTAN BigQuery Natural Language Query ===")
    click.echo()
    click.echo(f"USER QUESTION: {question}")
    click.echo()
    click.echo(TABLE_SCHEMAS_SUMMARY)
    click.echo()
    click.echo("=== INSTRUCTIONS ===")
    click.echo("Generate a safe read-only SQL query against isb-cgc-bq.HTAN tables.")
    click.echo('Then execute with: htan query bq sql "YOUR_SQL_HERE"')


def _bq_client_or_exit(project):
    try:
        return BigQueryClient(project=project)
    except BigQueryError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.exceptions.Exit(1)


@bq.command(name="sql")
@click.argument("sql_query")
@click.option("--project", "-p", help="Google Cloud project ID")
@click.option("--format", "-f", "fmt", type=click.Choice(["text", "json", "csv"]), default="text")
@click.option("--dry-run", "dry_run", is_flag=True)
def sql_cmd(sql_query, project, fmt, dry_run):
    """Execute a direct SQL query."""
    client = _bq_client_or_exit(project)
    try:
        if dry_run:
            result = client.query(sql_query, dry_run=True)
            bytes_est = result["bytes_processed"]
            if bytes_est > 1_000_000_000:
                cost_str = f"{bytes_est / 1_000_000_000:.2f} GB"
            elif bytes_est > 1_000_000:
                cost_str = f"{bytes_est / 1_000_000:.1f} MB"
            else:
                cost_str = f"{bytes_est:,} bytes"
            click.echo(f"Dry run — estimated data processed: {cost_str}", err=True)
            click.echo(f"SQL:\n{result['sql']}", err=True)
            return

        df = client.query(sql_query)
        if df.empty:
            click.echo("Query returned no results.", err=True)
            return
        click.echo(f"Returned {len(df)} rows, {len(df.columns)} columns", err=True)
        if fmt == "json":
            click.echo(df.to_json(orient="records", indent=2))
        elif fmt == "csv":
            output = io.StringIO()
            df.to_csv(output, index=False, quoting=csv.QUOTE_NONNUMERIC)
            click.echo(output.getvalue())
        else:
            click.echo(df.to_string(index=False))
    except BigQueryError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.exceptions.Exit(1)


@bq.command(name="tables")
@click.option("--project", "-p", help="Google Cloud project ID")
@click.option("--dry-run", "dry_run", is_flag=True)
@click.option("--versioned", is_flag=True, help="Use the HTAN_versioned dataset")
def tables_cmd(project, dry_run, versioned):
    """List available HTAN tables."""
    if dry_run:
        dataset = HTAN_DATASET_VERSIONED if versioned else HTAN_DATASET
        click.echo(f"Dry run — would list tables from {dataset}", err=True)
        return
    client = _bq_client_or_exit(project)
    try:
        rows = client.list_tables(versioned=versioned)
        for t in rows:
            click.echo(t)
        click.echo(f"\n{len(rows)} tables", err=True)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.exceptions.Exit(1)


@bq.command(name="describe")
@click.argument("table_name")
@click.option("--project", "-p", help="Google Cloud project ID")
@click.option("--dry-run", "dry_run", is_flag=True)
@click.option("--versioned", is_flag=True, help="Use the HTAN_versioned dataset")
def describe_cmd(table_name, project, dry_run, versioned):
    """Describe table schema."""
    if dry_run:
        click.echo(f"Dry run — would describe: {table_name}", err=True)
        return
    client = _bq_client_or_exit(project)
    try:
        info = client.describe_table(table_name, versioned=versioned)
        click.echo(f"Table: {info['table']}")
        click.echo(f"Rows: {info['num_rows']:,}")
        click.echo(f"Size: {info['num_bytes']:,} bytes")
        if info["description"]:
            click.echo(f"Description: {info['description']}")
        click.echo()
        click.echo(f"{'Column':<40} {'Type':<15} {'Mode':<10} {'Description'}")
        click.echo(f"{'-'*40} {'-'*15} {'-'*10} {'-'*30}")
        for f in info["schema"]:
            desc = f["description"]
            if len(desc) > 50:
                desc = desc[:47] + "..."
            click.echo(f"{f['name']:<40} {f['type']:<15} {f['mode']:<10} {desc}")
        click.echo(f"\n{len(info['schema'])} columns", err=True)
    except BigQueryError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.exceptions.Exit(1)


def cli_main(argv=None):
    """Backward-compatible entry point — invokes the Click :data:`bq` group."""
    try:
        return bq.main(args=argv, prog_name="htan query bq", standalone_mode=False)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except click.exceptions.ClickException as e:
        e.show()
        sys.exit(e.exit_code)
