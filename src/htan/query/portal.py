"""Query HTAN data via the HTAN Data Portal's ClickHouse backend.

The HTAN data portal (data.humantumoratlas.org) uses a ClickHouse cloud database.
Credentials are loaded via :mod:`htan.config` (3-tier: env > keychain > config file).
Queries go through the HTTP interface — zero extra dependencies (stdlib only).

Usage as library::

    from htan.query.portal import PortalClient
    client = PortalClient()
    files = client.find_files(organ="Breast", assay="scRNA-seq", limit=10)
    rows = client.query("SELECT count() FROM files")

Usage as CLI::

    htan query portal tables
    htan query portal files --organ Breast --limit 5
    htan query portal sql "SELECT atlas_name, COUNT(*) as n FROM files GROUP BY atlas_name"
"""

import base64
import csv
import io
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import click

from htan.config import (
    ConfigError,
    get_clickhouse_url,
    get_default_database,
    load_portal_config,
)

DEFAULT_LIMIT = 100
SQL_DEFAULT_LIMIT = 1000

# SQL keywords that indicate write/destructive operations — block these
BLOCKED_SQL_KEYWORDS = [
    "DELETE", "DROP", "UPDATE", "INSERT", "CREATE",
    "ALTER", "TRUNCATE", "MERGE", "GRANT", "REVOKE",
]

# SQL keywords that indicate read operations — allow these
ALLOWED_SQL_STARTS = ["SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN", "EXISTS"]

TABLE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")

# Columns that are Array(String) in the files table — need arrayExists() instead of ILIKE.
FILES_ARRAY_COLUMNS = {
    "organType", "Gender", "Ethnicity", "Race", "VitalStatus",
    "TreatmentType", "PrimaryDiagnosis", "TissueorOrganofOrigin",
    "biospecimenIds", "publicationIds", "diagnosisIds", "demographicsIds", "therapyIds",
}


class PortalError(Exception):
    """Error from HTAN portal operations (query failures, connection issues)."""
    def __init__(self, message, hints=None):
        super().__init__(message)
        self.hints = hints or []


# --- SQL helpers ---

def normalize_sql(sql):
    """Normalize SQL for ClickHouse compatibility.

    ClickHouse doesn't support the != operator — use <> instead.
    Also handles \\!= which occurs when shells escape ! inside double-quoted strings.
    """
    sql = sql.replace('\\!=', '<>')
    sql = sql.replace('!=', '<>')
    return sql


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


def validate_table_name(name):
    """Validate table name contains only safe characters. Raises ValueError."""
    if not TABLE_NAME_PATTERN.match(name):
        raise ValueError(f"Invalid table name '{name}'. Use only alphanumeric and underscores.")
    return name


def escape_sql_string(s):
    """Escape a string value for safe inclusion in SQL. Returns the escaped string without quotes."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def ensure_limit(sql, limit=DEFAULT_LIMIT):
    """Add LIMIT clause if none present."""
    normalized = " ".join(sql.upper().split())
    if "LIMIT" not in normalized:
        sql = sql.rstrip().rstrip(";")
        sql += f"\nLIMIT {limit}"
        print(f"Auto-applied LIMIT {limit}", file=sys.stderr)
    return sql


def build_where_clauses(filters, array_columns=None):
    """Build WHERE clause fragments from a dict of {column: value} filters.

    Args:
        filters: Dict mapping column names to filter values. None values are skipped.
        array_columns: Set of column names that are Array(String) and need arrayExists().

    Returns:
        List of SQL condition strings.
    """
    if array_columns is None:
        array_columns = set()
    clauses = []
    for col, val in filters.items():
        if val is not None:
            escaped = escape_sql_string(val)
            if col in array_columns:
                clauses.append(f"arrayExists(x -> x ILIKE '%{escaped}%', {col})")
            else:
                clauses.append(f"{col} ILIKE '%{escaped}%'")
    return clauses


# --- Low-level query functions ---

def _make_ssl_context():
    """Create an SSL context, trying certifi first."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def clickhouse_query(sql, fmt="JSONEachRow", database=None, timeout=60, config=None):
    """Execute a read-only SQL query against the ClickHouse HTTP interface.

    Args:
        sql: SQL query string
        fmt: ClickHouse output format (JSONEachRow, TabSeparated, CSV, etc.)
        database: Database name to query against (None = use config default)
        timeout: HTTP request timeout in seconds (default: 60)
        config: Portal config dict. If None, loads from default config file.

    Returns:
        Raw response body as string.

    Raises:
        PortalError on HTTP, connection, or timeout errors.
    """
    sql = normalize_sql(sql)

    cfg = config if config is not None else load_portal_config()

    params = {"default_format": fmt}
    if database is not None:
        params["database"] = database

    url = get_clickhouse_url(cfg) + "?" + urllib.parse.urlencode(params)

    credentials = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()

    req = urllib.request.Request(
        url,
        data=sql.encode("utf-8"),
        headers={"Authorization": f"Basic {credentials}"},
        method="POST",
    )

    ctx = _make_ssl_context()

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            pass
        clean_msg = error_body[:500]
        if error_body.startswith("{"):
            try:
                err_json = json.loads(error_body)
                clean_msg = err_json.get("exception", clean_msg)
            except (json.JSONDecodeError, KeyError):
                pass
        hints = []
        if "Unrecognized token" in clean_msg and "!=" in clean_msg:
            hints.append("Use <> instead of != for not-equal comparisons in ClickHouse")
        if "UNKNOWN_IDENTIFIER" in clean_msg or "Missing columns" in clean_msg:
            hints.append("Run 'describe <table>' to see available column names")
        if "CANNOT_PARSE_TEXT" in clean_msg or "CANNOT_PARSE_INPUT" in clean_msg:
            hints.append("Use toInt32OrNull() or toFloat64OrNull() for columns with non-numeric values")
        if "Array" in clean_msg and ("ILLEGAL_TYPE" in clean_msg or "argument of function" in clean_msg):
            hints.append("Use arrayExists() or arrayJoin() for Array(String) columns like organType, Gender, Race")
        raise PortalError(f"ClickHouse HTTP {e.code}: {clean_msg}", hints=hints)
    except urllib.error.URLError as e:
        raise PortalError(
            f"Could not connect to HTAN portal ClickHouse: {e.reason}\n"
            "The portal endpoint may be temporarily unavailable."
        )
    except TimeoutError:
        raise PortalError(f"Query timed out after {timeout}s. Try a simpler query or add a LIMIT clause.")
    except PortalError:
        raise
    except Exception as e:
        raise PortalError(str(e))


def parse_json_rows(response_text):
    """Parse JSONEachRow response into a list of dicts."""
    if not response_text or not response_text.strip():
        return []

    rows = []
    error_lines = []
    for line in response_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            error_lines.append(line)

    if not rows and error_lines:
        error_text = "\n".join(error_lines[:5])
        raise PortalError(f"ClickHouse returned non-JSON response:\n{error_text}")

    if error_lines:
        print(f"Warning: {len(error_lines)} non-JSON line(s) in response", file=sys.stderr)

    return rows


def discover_database(config=None):
    """Discover the latest HTAN database by querying SHOW DATABASES.

    Args:
        config: Portal config dict. If None, loads from default config file.
    """
    cfg = config if config is not None else load_portal_config()
    config_default = get_default_database(cfg)

    try:
        resp = clickhouse_query("SHOW DATABASES LIKE 'htan_%'", fmt="TabSeparated", database="", config=cfg)
        if not resp.strip():
            return config_default

        databases = [line.strip() for line in resp.strip().split("\n") if line.strip()]
        htan_dbs = sorted([db for db in databases if db.startswith("htan_")], reverse=True)
        if htan_dbs:
            latest = htan_dbs[0]
            if config_default and latest != config_default:
                print(f"Discovered newer database: {latest} (config default was {config_default})", file=sys.stderr)
            return latest
    except Exception:
        pass

    return config_default


# --- Output formatting ---

def _format_cell_value(val):
    """Format a cell value for text table display."""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    return str(val)


def format_text_table(rows):
    """Format rows as an aligned text table."""
    if not rows:
        return ""

    columns = list(rows[0].keys())

    formatted = []
    for row in rows:
        formatted.append({col: _format_cell_value(row.get(col, "")) for col in columns})

    widths = {}
    for col in columns:
        widths[col] = max(
            len(col),
            max((len(frow[col]) for frow in formatted), default=0),
        )

    try:
        term_width = os.get_terminal_size().columns
    except (AttributeError, ValueError, OSError):
        term_width = 200

    if len(columns) <= 3:
        max_col_width = max(term_width // 2, 80)
    elif len(columns) <= 6:
        max_col_width = max(term_width // len(columns), 40)
    else:
        max_col_width = max(term_width // len(columns), 20)

    truncated = False
    for col in columns:
        if widths[col] > max_col_width:
            widths[col] = max_col_width
            truncated = True

    header = "  ".join(f"{col:<{widths[col]}}" for col in columns)
    sep = "  ".join("-" * widths[col] for col in columns)

    lines = [header, sep]
    for frow in formatted:
        parts = []
        for col in columns:
            val = frow[col]
            if len(val) > widths[col]:
                val = val[: widths[col] - 3] + "..."
                truncated = True
            parts.append(f"{val:<{widths[col]}}")
        lines.append("  ".join(parts))

    if truncated:
        print("Hint: Some values were truncated. Use --output json for full values.", file=sys.stderr)

    return "\n".join(lines)


def format_output(rows, output_format="text"):
    """Format rows in the requested output format."""
    if not rows:
        print("No results.", file=sys.stderr)
        return

    if output_format == "json":
        print(json.dumps(rows, indent=2))
    elif output_format == "csv":
        if rows:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=rows[0].keys(), quoting=csv.QUOTE_NONNUMERIC)
            writer.writeheader()
            writer.writerows(rows)
            print(output.getvalue(), end="")
    else:
        print(format_text_table(rows))


# --- PortalClient class ---

class PortalClient:
    """High-level client for HTAN portal ClickHouse queries.

    Config is auto-loaded from ~/.config/htan-skill/portal.json if not provided.

    Usage:
        client = PortalClient()
        files = client.find_files(organ="Breast", limit=10)
        rows = client.query("SELECT count() FROM files")
    """

    def __init__(self, config=None):
        """Initialize the portal client.

        Args:
            config: Portal config dict. If None, loads via 3-tier resolution.
        """
        self._config = config
        self._database = None

    def _cfg(self):
        if self._config is None:
            self._config = load_portal_config()
        return self._config

    def _db(self):
        if self._database is None:
            self._database = discover_database(config=self._cfg())
        return self._database

    def query(self, sql, limit=SQL_DEFAULT_LIMIT):
        """Execute a read-only SQL query. Returns list of dicts."""
        safe, reason = validate_sql_safety(sql)
        if not safe:
            raise PortalError(f"{reason}\nOnly read-only queries are allowed.")
        sql = ensure_limit(sql, limit)
        resp = clickhouse_query(sql, database=self._db(), config=self._cfg())
        return parse_json_rows(resp)

    def find_files(self, organ=None, assay=None, atlas=None, level=None,
                   file_format=None, filename=None, data_file_id=None, limit=DEFAULT_LIMIT):
        """Search the files table with optional filters. Returns list of dicts."""
        columns = [
            "DataFileID", "Filename", "FileFormat", "assayName", "level",
            "organType", "atlas_name", "synapseId",
            "JSONExtractString(viewers, 'crdcGc', 'drs_uri') as drs_uri",
            "downloadSource",
        ]

        filters = {
            "organType": organ, "assayName": assay, "atlas_name": atlas,
            "level": level, "FileFormat": file_format, "Filename": filename,
        }

        where = build_where_clauses(filters, array_columns=FILES_ARRAY_COLUMNS)

        if data_file_id:
            ids = [data_file_id] if isinstance(data_file_id, str) else data_file_id
            escaped_ids = ", ".join(f"'{escape_sql_string(fid)}'" for fid in ids)
            where.append(f"DataFileID IN ({escaped_ids})")

        sql = f"SELECT {', '.join(columns)} FROM files"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f"\nLIMIT {limit}"

        resp = clickhouse_query(sql, database=self._db(), config=self._cfg())
        return parse_json_rows(resp)

    def list_tables(self):
        """List available tables in the HTAN ClickHouse database."""
        resp = clickhouse_query("SHOW TABLES", fmt="TabSeparated",
                                database=self._db(), config=self._cfg())
        return sorted([line.strip() for line in resp.strip().split("\n") if line.strip()])

    def describe_table(self, table):
        """Describe the schema of a table. Returns list of column dicts."""
        validate_table_name(table)
        resp = clickhouse_query(f"DESCRIBE {table}", fmt="JSONEachRow",
                                database=self._db(), config=self._cfg())
        schema = parse_json_rows(resp)

        row_count = None
        try:
            count_resp = clickhouse_query(f"SELECT count() as cnt FROM {table}",
                                          database=self._db(), config=self._cfg())
            count_rows = parse_json_rows(count_resp)
            row_count = count_rows[0].get("cnt") if count_rows else None
        except PortalError:
            pass

        columns = [
            {
                "name": row.get("name", ""),
                "type": row.get("type", ""),
                "default_expression": row.get("default_expression", ""),
                "comment": row.get("comment", ""),
            }
            for row in schema
        ]

        return {"table": table, "row_count": row_count, "columns": columns,
                "column_count": len(columns), "database": self._db()}

    def get_demographics(self, atlas=None, organ=None, limit=DEFAULT_LIMIT):
        """Query the demographics table."""
        return self._clinical_query("demographics", atlas=atlas, limit=limit)

    def get_diagnosis(self, atlas=None, organ=None, limit=DEFAULT_LIMIT):
        """Query the diagnosis table."""
        return self._clinical_query("diagnosis", atlas=atlas, organ=organ, limit=limit)

    def get_manifest(self, file_ids):
        """Look up files and return download coordinates (synapseId, drs_uri)."""
        if not file_ids:
            return []
        escaped_ids = ", ".join(f"'{escape_sql_string(fid)}'" for fid in file_ids)
        sql = (
            "SELECT DataFileID, Filename, synapseId, "
            "JSONExtractString(viewers, 'crdcGc', 'drs_uri') as drs_uri, "
            "downloadSource "
            "FROM files "
            f"WHERE DataFileID IN ({escaped_ids})"
        )
        resp = clickhouse_query(sql, database=self._db(), config=self._cfg())
        return parse_json_rows(resp)

    def summary(self):
        """Get overview statistics (file/participant counts by atlas, assay, organ)."""
        queries = {
            "files_by_atlas": "SELECT atlas_name, count() as file_count FROM files GROUP BY atlas_name ORDER BY file_count DESC",
            "files_by_assay": "SELECT assayName, count() as file_count FROM files GROUP BY assayName ORDER BY file_count DESC",
            "files_by_organ": "SELECT arrayJoin(organType) as organ, count() as file_count FROM files GROUP BY organ ORDER BY file_count DESC",
            "participants_by_atlas": "SELECT atlas_name, count() as participant_count FROM demographics GROUP BY atlas_name ORDER BY participant_count DESC",
            "total_files": "SELECT count() as total FROM files",
            "total_participants": "SELECT count() as total FROM demographics",
        }

        results = {}
        for label, sql in queries.items():
            try:
                resp = clickhouse_query(sql, database=self._db(), config=self._cfg())
                results[label] = parse_json_rows(resp)
            except PortalError:
                results[label] = []

        total_files = results.get("total_files", [{}])
        total_participants = results.get("total_participants", [{}])

        return {
            "database": self._db(),
            "total_files": total_files[0].get("total", 0) if total_files else 0,
            "total_participants": total_participants[0].get("total", 0) if total_participants else 0,
            "files_by_atlas": results.get("files_by_atlas", []),
            "files_by_assay": results.get("files_by_assay", []),
            "files_by_organ": results.get("files_by_organ", []),
            "participants_by_atlas": results.get("participants_by_atlas", []),
        }

    def _clinical_query(self, table, atlas=None, organ=None, limit=DEFAULT_LIMIT):
        """Query a clinical table with optional filters."""
        valid_tables = {"demographics", "diagnosis", "cases", "specimen"}
        if table not in valid_tables:
            raise ValueError(f"Invalid table '{table}'. Must be one of: {', '.join(sorted(valid_tables))}")

        filters = {}
        if atlas:
            filters["atlas_name"] = atlas
        if organ:
            if table in ("diagnosis", "cases", "specimen"):
                filters["TissueorOrganofOrigin"] = organ

        where = build_where_clauses(filters)

        sql = f"SELECT * FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f"\nLIMIT {limit}"

        resp = clickhouse_query(sql, database=self._db(), config=self._cfg())
        return parse_json_rows(resp)


# --- CLI ---

def _run(handler, args):
    """Run a portal command handler with consistent error handling."""
    try:
        handler(args)
    except PortalError as e:
        click.echo(f"Error: {e}", err=True)
        for hint in e.hints:
            click.echo(f"Hint: {hint}", err=True)
        raise click.exceptions.Exit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.exceptions.Exit(1)


_PORTAL_EPILOG = """\
Examples:

  htan query portal tables
  htan query portal describe files
  htan query portal files --organ Breast --assay "scRNA-seq" --limit 5
  htan query portal files --data-file-id HTA9_1_19512 --output json
  htan query portal sql "SELECT atlas_name, COUNT(*) as n FROM files GROUP BY atlas_name"
  htan query portal manifest HTA9_1_19512 --output-dir /tmp/manifests
"""


@click.group(epilog=_PORTAL_EPILOG)
def portal():
    """Query HTAN data via the portal ClickHouse backend."""


@portal.command()
@click.option("--organ", help="Filter by organ type")
@click.option("--assay", help="Filter by assay name")
@click.option("--atlas", help="Filter by atlas name")
@click.option("--level", help="Filter by data level")
@click.option("--file-format", "file_format", help="Filter by file format")
@click.option("--filename", help="Filter by filename (substring)")
@click.option("--data-file-id", "data_file_id", multiple=True,
              help="Look up specific HTAN_Data_File_ID(s)")
@click.option("--limit", "-l", type=int, default=DEFAULT_LIMIT, show_default=True,
              help="Row limit")
@click.option("--output", "-o", type=click.Choice(["text", "json", "csv"]),
              default="text", show_default=True, help="Output format")
@click.option("--dry-run", "dry_run", is_flag=True, help="Show SQL without executing")
@click.option("--database", "-d", help="Database name (default: auto-discover)")
def files(organ, assay, atlas, level, file_format, filename, data_file_id,
          limit, output, dry_run, database):
    """Query files with filters."""
    args = SimpleNamespace(
        organ=organ, assay=assay, atlas=atlas, level=level,
        file_format=file_format, filename=filename,
        data_file_id=list(data_file_id) if data_file_id else None,
        limit=limit, output=output, dry_run=dry_run, database=database,
    )
    _run(_cmd_files, args)


@portal.command()
@click.option("--atlas", help="Filter by atlas name")
@click.option("--gender", help="Filter by gender")
@click.option("--race", help="Filter by race")
@click.option("--limit", "-l", type=int, default=DEFAULT_LIMIT, show_default=True)
@click.option("--output", "-o", type=click.Choice(["text", "json", "csv"]), default="text")
@click.option("--dry-run", "dry_run", is_flag=True)
@click.option("--database", "-d")
def demographics(atlas, gender, race, limit, output, dry_run, database):
    """Query patient demographics."""
    args = SimpleNamespace(atlas=atlas, gender=gender, race=race,
                           limit=limit, output=output, dry_run=dry_run, database=database)
    _run(_cmd_demographics, args)


@portal.command()
@click.option("--atlas", help="Filter by atlas name")
@click.option("--organ", help="Filter by tissue/organ of origin")
@click.option("--primary-diagnosis", "primary_diagnosis", help="Filter by primary diagnosis")
@click.option("--limit", "-l", type=int, default=DEFAULT_LIMIT, show_default=True)
@click.option("--output", "-o", type=click.Choice(["text", "json", "csv"]), default="text")
@click.option("--dry-run", "dry_run", is_flag=True)
@click.option("--database", "-d")
def diagnosis(atlas, organ, primary_diagnosis, limit, output, dry_run, database):
    """Query diagnosis information."""
    args = SimpleNamespace(atlas=atlas, organ=organ, primary_diagnosis=primary_diagnosis,
                           limit=limit, output=output, dry_run=dry_run, database=database)
    _run(_cmd_diagnosis, args)


@portal.command()
@click.option("--atlas", help="Filter by atlas name")
@click.option("--organ", help="Filter by tissue/organ of origin")
@click.option("--limit", "-l", type=int, default=DEFAULT_LIMIT, show_default=True)
@click.option("--output", "-o", type=click.Choice(["text", "json", "csv"]), default="text")
@click.option("--dry-run", "dry_run", is_flag=True)
@click.option("--database", "-d")
def cases(atlas, organ, limit, output, dry_run, database):
    """Query merged cases."""
    args = SimpleNamespace(atlas=atlas, organ=organ, limit=limit, output=output,
                           dry_run=dry_run, database=database)
    _run(_cmd_cases, args)


@portal.command()
@click.option("--atlas", help="Filter by atlas name")
@click.option("--preservation", help="Filter by preservation method")
@click.option("--tissue-type", "tissue_type", help="Filter by tumor tissue type")
@click.option("--limit", "-l", type=int, default=DEFAULT_LIMIT, show_default=True)
@click.option("--output", "-o", type=click.Choice(["text", "json", "csv"]), default="text")
@click.option("--dry-run", "dry_run", is_flag=True)
@click.option("--database", "-d")
def specimen(atlas, preservation, tissue_type, limit, output, dry_run, database):
    """Query biospecimen metadata."""
    args = SimpleNamespace(atlas=atlas, preservation=preservation, tissue_type=tissue_type,
                           limit=limit, output=output, dry_run=dry_run, database=database)
    _run(_cmd_specimen, args)


@portal.command()
@click.option("--output", "-o", type=click.Choice(["text", "json"]), default="text")
@click.option("--dry-run", "dry_run", is_flag=True)
@click.option("--database", "-d")
def summary(output, dry_run, database):
    """Show HTAN data summary."""
    args = SimpleNamespace(output=output, dry_run=dry_run, database=database)
    _run(_cmd_summary, args)


@portal.command(name="sql")
@click.argument("sql_query")
@click.option("--limit", "-l", type=int, default=SQL_DEFAULT_LIMIT, show_default=True,
              help="Row limit")
@click.option("--no-limit", "no_limit", is_flag=True, help="Skip auto-applying LIMIT")
@click.option("--output", "-o", type=click.Choice(["text", "json", "csv"]), default="text")
@click.option("--dry-run", "dry_run", is_flag=True)
@click.option("--database", "-d")
def sql_cmd(sql_query, limit, no_limit, output, dry_run, database):
    """Execute a direct read-only SQL query."""
    args = SimpleNamespace(sql=sql_query, limit=limit, no_limit=no_limit,
                           output=output, dry_run=dry_run, database=database)
    _run(_cmd_sql, args)


@portal.command()
@click.option("--dry-run", "dry_run", is_flag=True)
@click.option("--database", "-d")
def tables(dry_run, database):
    """List available tables."""
    args = SimpleNamespace(dry_run=dry_run, database=database)
    _run(_cmd_tables, args)


@portal.command()
@click.argument("table_name")
@click.option("--dry-run", "dry_run", is_flag=True)
@click.option("--database", "-d")
def describe(table_name, dry_run, database):
    """Describe table schema."""
    args = SimpleNamespace(table_name=table_name, dry_run=dry_run, database=database)
    _run(_cmd_describe, args)


@portal.command()
@click.argument("ids", nargs=-1)
@click.option("--file", "-f", "file", help="File containing IDs (one per line)")
@click.option("--output-dir", "output_dir", default=".", show_default=True,
              help="Directory for manifest files")
@click.option("--dry-run", "dry_run", is_flag=True)
@click.option("--database", "-d")
def manifest(ids, file, output_dir, dry_run, database):
    """Generate download manifests from file IDs."""
    args = SimpleNamespace(ids=list(ids), file=file, output_dir=output_dir,
                           dry_run=dry_run, database=database)
    _run(_cmd_manifest, args)


def cli_main(argv=None):
    """Backward-compatible entry point — invokes the Click :data:`portal` group.

    Args:
        argv: List of CLI arguments (e.g., ``["tables"]``). If ``None``, uses ``sys.argv``.

    Returns ``None`` on success; raises :class:`SystemExit` on errors or when a
    Click action (such as ``--help``) requests an exit.
    """
    try:
        return portal.main(args=argv, prog_name="htan query portal", standalone_mode=False)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except click.exceptions.ClickException as e:
        e.show()
        sys.exit(e.exit_code)


# --- CLI handler functions (mirror original cmd_* functions) ---

def _cmd_files(args):
    columns = [
        "DataFileID", "Filename", "FileFormat", "assayName", "level",
        "organType", "atlas_name", "synapseId",
        "JSONExtractString(viewers, 'crdcGc', 'drs_uri') as drs_uri",
        "downloadSource",
    ]
    filters = {
        "organType": args.organ, "assayName": args.assay, "atlas_name": args.atlas,
        "level": args.level, "FileFormat": args.file_format, "Filename": args.filename,
    }
    where = build_where_clauses(filters, array_columns=FILES_ARRAY_COLUMNS)
    if args.data_file_id:
        ids = [args.data_file_id] if isinstance(args.data_file_id, str) else args.data_file_id
        escaped_ids = ", ".join(f"'{escape_sql_string(fid)}'" for fid in ids)
        where.append(f"DataFileID IN ({escaped_ids})")

    sql = f"SELECT {', '.join(columns)} FROM files"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f"\nLIMIT {args.limit}"

    database = args.database or discover_database()
    if args.dry_run:
        print(f"Database: {database}", file=sys.stderr)
        print(f"SQL:\n{sql}", file=sys.stderr)
        return

    print(f"Querying files in {database}...", file=sys.stderr)
    resp = clickhouse_query(sql, database=database)
    rows = parse_json_rows(resp)
    print(f"Returned {len(rows)} rows", file=sys.stderr)
    format_output(rows, args.output)


def _cmd_demographics(args):
    filters = {"atlas_name": args.atlas, "Gender": args.gender, "Race": args.race}
    where = build_where_clauses(filters)
    sql = "SELECT * FROM demographics"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f"\nLIMIT {args.limit}"
    database = args.database or discover_database()
    if args.dry_run:
        print(f"Database: {database}\nSQL:\n{sql}", file=sys.stderr)
        return
    print(f"Querying demographics in {database}...", file=sys.stderr)
    resp = clickhouse_query(sql, database=database)
    rows = parse_json_rows(resp)
    print(f"Returned {len(rows)} rows", file=sys.stderr)
    format_output(rows, args.output)


def _cmd_diagnosis(args):
    filters = {"atlas_name": args.atlas, "TissueorOrganofOrigin": args.organ, "PrimaryDiagnosis": args.primary_diagnosis}
    where = build_where_clauses(filters)
    sql = "SELECT * FROM diagnosis"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f"\nLIMIT {args.limit}"
    database = args.database or discover_database()
    if args.dry_run:
        print(f"Database: {database}\nSQL:\n{sql}", file=sys.stderr)
        return
    print(f"Querying diagnosis in {database}...", file=sys.stderr)
    resp = clickhouse_query(sql, database=database)
    rows = parse_json_rows(resp)
    print(f"Returned {len(rows)} rows", file=sys.stderr)
    format_output(rows, args.output)


def _cmd_cases(args):
    filters = {"atlas_name": args.atlas, "TissueorOrganofOrigin": args.organ}
    where = build_where_clauses(filters)
    sql = "SELECT * FROM cases"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f"\nLIMIT {args.limit}"
    database = args.database or discover_database()
    if args.dry_run:
        print(f"Database: {database}\nSQL:\n{sql}", file=sys.stderr)
        return
    print(f"Querying cases in {database}...", file=sys.stderr)
    resp = clickhouse_query(sql, database=database)
    rows = parse_json_rows(resp)
    print(f"Returned {len(rows)} rows", file=sys.stderr)
    format_output(rows, args.output)


def _cmd_specimen(args):
    filters = {"atlas_name": args.atlas, "PreservationMethod": args.preservation, "TumorTissueType": args.tissue_type}
    where = build_where_clauses(filters)
    sql = "SELECT * FROM specimen"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f"\nLIMIT {args.limit}"
    database = args.database or discover_database()
    if args.dry_run:
        print(f"Database: {database}\nSQL:\n{sql}", file=sys.stderr)
        return
    print(f"Querying specimen in {database}...", file=sys.stderr)
    resp = clickhouse_query(sql, database=database)
    rows = parse_json_rows(resp)
    print(f"Returned {len(rows)} rows", file=sys.stderr)
    format_output(rows, args.output)


def _cmd_summary(args):
    database = args.database or discover_database()
    if args.dry_run:
        print(f"Database: {database}", file=sys.stderr)
        print("Would run summary aggregation queries", file=sys.stderr)
        return

    print(f"Querying summary from {database}...", file=sys.stderr)
    queries = {
        "Files by atlas": "SELECT atlas_name, count() as file_count FROM files GROUP BY atlas_name ORDER BY file_count DESC",
        "Files by assay": "SELECT assayName, count() as file_count FROM files GROUP BY assayName ORDER BY file_count DESC",
        "Files by organ": "SELECT arrayJoin(organType) as organ, count() as file_count FROM files GROUP BY organ ORDER BY file_count DESC",
        "Participants by atlas": "SELECT atlas_name, count() as participant_count FROM demographics GROUP BY atlas_name ORDER BY participant_count DESC",
        "Total files": "SELECT count() as total FROM files",
        "Total participants": "SELECT count() as total FROM demographics",
    }
    results = {}
    for label, sql in queries.items():
        try:
            resp = clickhouse_query(sql, database=database)
            results[label] = parse_json_rows(resp)
        except PortalError:
            results[label] = []

    if args.output == "json":
        print(json.dumps(results, indent=2))
        return

    total_files = results.get("Total files", [{}])[0].get("total", "?")
    total_participants = results.get("Total participants", [{}])[0].get("total", "?")
    print(f"HTAN Portal Summary (database: {database})")
    print(f"Total files: {total_files:,}" if isinstance(total_files, int) else f"Total files: {total_files}")
    print(f"Total participants: {total_participants:,}" if isinstance(total_participants, int) else f"Total participants: {total_participants}")
    print()
    for label in ["Files by atlas", "Files by assay", "Files by organ", "Participants by atlas"]:
        rows = results.get(label, [])
        if rows:
            print(f"--- {label} ---")
            print(format_text_table(rows))
            print()


def _cmd_sql(args):
    sql = args.sql
    safe, reason = validate_sql_safety(sql)
    if not safe:
        raise PortalError(f"{reason}\nOnly read-only queries are allowed.")
    no_limit = getattr(args, "no_limit", False)
    limit = args.limit if hasattr(args, "limit") else SQL_DEFAULT_LIMIT
    if not no_limit:
        sql = ensure_limit(sql, limit)
    database = args.database or discover_database()
    if args.dry_run:
        print(f"Database: {database}\nSQL:\n{sql}", file=sys.stderr)
        return
    print(f"Executing query in {database}...", file=sys.stderr)
    resp = clickhouse_query(sql, database=database)
    rows = parse_json_rows(resp)
    print(f"Returned {len(rows)} rows", file=sys.stderr)
    if not no_limit and len(rows) == limit:
        print(f"Warning: Result count ({len(rows)}) matches limit. Use --no-limit or higher --limit.", file=sys.stderr)
    format_output(rows, args.output)


def _cmd_tables(args):
    database = args.database or discover_database()
    if args.dry_run:
        print(f"Database: {database}\nSQL: SHOW TABLES", file=sys.stderr)
        return
    print(f"Listing tables in {database}...", file=sys.stderr)
    resp = clickhouse_query("SHOW TABLES", fmt="TabSeparated", database=database)
    tables = [line.strip() for line in resp.strip().split("\n") if line.strip()]
    for t in sorted(tables):
        print(t)
    print(f"\n{len(tables)} tables", file=sys.stderr)


def _cmd_describe(args):
    table_name = validate_table_name(args.table_name)
    database = args.database or discover_database()
    if args.dry_run:
        print(f"Database: {database}\nSQL: DESCRIBE {table_name}", file=sys.stderr)
        return
    print(f"Describing {table_name} in {database}...", file=sys.stderr)
    resp = clickhouse_query(f"DESCRIBE {table_name}", fmt="JSONEachRow", database=database)
    rows = parse_json_rows(resp)
    if not rows:
        print(f"No schema found for table '{table_name}'.", file=sys.stderr)
        sys.exit(1)

    count_resp = clickhouse_query(f"SELECT count() as cnt FROM {table_name}", database=database)
    count_rows = parse_json_rows(count_resp)
    row_count = count_rows[0].get("cnt", "?") if count_rows else "?"

    print(f"Table: {database}.{table_name}")
    print(f"Rows: {row_count:,}" if isinstance(row_count, int) else f"Rows: {row_count}")
    print()
    print(f"{'Column':<40} {'Type':<30} {'Default':<15} {'Comment'}")
    print(f"{'-'*40} {'-'*30} {'-'*15} {'-'*30}")
    for row in rows:
        name = row.get("name", "")
        dtype = row.get("type", "")
        default = row.get("default_expression", "") or ""
        comment = row.get("comment", "") or ""
        if len(comment) > 40:
            comment = comment[:37] + "..."
        print(f"{name:<40} {dtype:<30} {default:<15} {comment}")
    print(f"\n{len(rows)} columns", file=sys.stderr)


def _cmd_manifest(args):
    file_ids = list(args.ids) if args.ids else []
    if args.file:
        try:
            with open(args.file, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        file_ids.append(line)
        except FileNotFoundError:
            print(f"Error: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)

    if not file_ids:
        print("Error: No file IDs provided.", file=sys.stderr)
        sys.exit(1)

    database = args.database or discover_database()
    escaped_ids = ", ".join(f"'{escape_sql_string(fid)}'" for fid in file_ids)
    sql = f"""SELECT DataFileID, Filename, synapseId,
       JSONExtractString(viewers, 'crdcGc', 'drs_uri') as drs_uri,
       downloadSource
FROM files
WHERE DataFileID IN ({escaped_ids})"""

    if args.dry_run:
        print(f"Database: {database}\nSQL:\n{sql}", file=sys.stderr)
        print(f"Would generate manifests in: {args.output_dir}", file=sys.stderr)
        return

    print(f"Looking up {len(file_ids)} file(s) in {database}...", file=sys.stderr)
    resp = clickhouse_query(sql, database=database)
    rows = parse_json_rows(resp)

    if not rows:
        print("No matching files found.", file=sys.stderr)
        sys.exit(1)

    found_ids = {r["DataFileID"] for r in rows}
    not_found = [fid for fid in file_ids if fid not in found_ids]
    if not_found:
        print(f"Not found ({len(not_found)}): {', '.join(not_found)}", file=sys.stderr)
    print(f"Found {len(rows)}/{len(file_ids)} files", file=sys.stderr)

    synapse_files = [r for r in rows if r.get("synapseId")]
    gen3_files = [r for r in rows if r.get("drs_uri")]

    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)
    files_written = []

    if synapse_files:
        manifest_path = os.path.join(output_dir, "synapse_manifest.tsv")
        with open(manifest_path, "w") as f:
            f.write("synapseId\tDataFileID\tFilename\n")
            for row in synapse_files:
                f.write(f"{row.get('synapseId', '')}\t{row.get('DataFileID', '')}\t{row.get('Filename', '')}\n")
        print(f"Synapse manifest: {manifest_path} ({len(synapse_files)} files)", file=sys.stderr)
        files_written.append(manifest_path)

    if gen3_files:
        manifest_path = os.path.join(output_dir, "gen3_manifest.json")
        manifest = []
        for row in gen3_files:
            drs = row.get("drs_uri", "")
            if not drs.startswith("drs://"):
                drs = f"drs://{drs}"
            manifest.append({"object_id": drs, "DataFileID": row.get("DataFileID", ""), "Filename": row.get("Filename", "")})
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"Gen3 manifest: {manifest_path} ({len(gen3_files)} files)", file=sys.stderr)
        files_written.append(manifest_path)

    print(json.dumps({
        "total_files": len(rows), "synapse_files": len(synapse_files),
        "gen3_files": len(gen3_files), "not_found": not_found, "manifests": files_written,
    }, indent=2))
