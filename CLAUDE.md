# htan-cli — developer guide

This repo holds the `htan` Python package — a CLI and library for accessing Human Tumor Atlas Network (HTAN) data. It is published to PyPI as `htan` and depended on by the Claude Code plugin at [`ncihtan/htan-claude`](https://github.com/ncihtan/htan-claude).

## Package layout

| Module | Source | Notes |
|---|---|---|
| `htan.config` | `src/htan/config.py` | 3-tier credential resolution (env > keychain > config file) |
| `htan.query.portal` | `src/htan/query/portal.py` | Portal ClickHouse queries, `PortalClient` class |
| `htan.query.bq` | `src/htan/query/bq.py` | BigQuery queries, `BigQueryClient` class |
| `htan.download.synapse` | `src/htan/download/synapse.py` | Synapse downloads |
| `htan.download.gen3` | `src/htan/download/gen3.py` | Gen3/CRDC DRS downloads |
| `htan.pubs` | `src/htan/pubs.py` | PubMed search |
| `htan.model` | `src/htan/model.py` | HTAN data model queries, `DataModel` class |
| `htan.files` | `src/htan/files.py` | File ID to download coordinate mapping |
| `htan.init` | `src/htan/init.py` | First-run setup logic |
| `htan.cli` | `src/htan/cli.py` | Unified CLI entry point (`htan` command) |

## Environment

The project uses a **uv virtual environment** and `pyproject.toml`-based packaging.

### uv rules

All Python dependencies **must** be managed with `uv`. Never use `pip`, `pip-tools`, `poetry`, or `conda`.

- Install for development: `uv pip install -e ".[dev]"`
- Run CLI: `uv run htan <command>`
- Run tests: `uv run pytest tests/`
- Run tools directly: `uvx ruff`, `uvx pytest`
- Build wheel: `uv build`
- Publish: `uv publish` (requires PYPI_TOKEN)

When executing any Python code, always use `uv run` instead of activating the venv manually.

## Credential security

Credentials are stored in config files, NOT environment variables:
- **Portal ClickHouse**: `~/.config/htan-skill/portal.json` (populated by `htan init`, fetched from Synapse project syn73720845 gated by Team:3574960 membership)
- **Synapse**: `~/.synapseConfig`
- **Gen3**: `~/.gen3/credentials.json`
- **BigQuery**: Application Default Credentials (`gcloud auth application-default login`)

When developing under Claude Code, avoid running commands that print credentials or signed URLs into the conversation:
- **Safe**: `--help`, `--dry-run`, all portal queries, BigQuery `tables`/`describe`/`sql`, file mapping `update`/`lookup`/`stats`, all `model` commands, all `pubs` commands
- **Run in your own terminal**: `htan download gen3 resolve` (outputs signed URLs), any command where errors might echo tokens

## Data access tiers

HTAN data has multiple access levels. The portal provides a unified query interface.

### Portal metadata + file discovery (ClickHouse)

- **Client**: stdlib only (`urllib`, `json`, `base64`, `ssl`)
- **Auth**: Credentials cached at `~/.config/htan-skill/portal.json` (fetched via `htan init`)
- **Data**: File metadata, download coordinates, basic clinical data
- **Tables**: `files`, `demographics`, `diagnosis`, `cases`, `specimen`, `atlases`, `publication_manifest`
- **Limitations**: No SLA, database name changes with releases, simpler schema than BigQuery

### Open access (Synapse)

- **Client**: `synapseclient`
- **Auth**: PAT via `SYNAPSE_AUTH_TOKEN` env var or `~/.synapseConfig`
- **Data**: De-identified clinical data, processed matrices, imaging metadata
- **Operations**: `syn.get(synapse_id)`, `synapseutils.syncFromSynapse()`

### Controlled access (CRDC/Gen3)

- **Client**: `gen3` SDK
- **Auth**: Gen3 credentials JSON from CRDC portal (after dbGaP authorization)
- **Endpoint**: `https://nci-crdc.datacommons.io`
- **Data**: Raw sequencing (FASTQs, BAMs), protected genomic data
- **Identifiers**: DRS URIs in format `drs://dg.4DFC/<guid>`

### Metadata query (ISB-CGC BigQuery)

- **Project**: `isb-cgc-bq`
- **Datasets**: `HTAN` (`_current` tables) or `HTAN_versioned` (`_rN` tables)
- **Auth**: Application Default Credentials
- **Key tables**:
  - `clinical_tier1_demographics_current`
  - `clinical_tier1_diagnosis_current`
  - `biospecimen_current`
  - `scRNAseq_level1_metadata_current` (also level 2-4)
  - `imaging_level2_metadata_current`

## Unified workflow

### Recommended: portal → download (2 steps)

The portal includes download coordinates directly:

```bash
htan query portal files --organ Breast --assay "scRNA-seq" --output json
htan query portal manifest HTA9_1_19512 --output-dir ./manifests
htan download synapse download syn26535909
```

### Alternative: BigQuery → file mapping → download (3 steps)

For complex multi-table joins:

```bash
htan query bq sql "SELECT HTAN_Data_File_ID FROM ... WHERE ..."
htan files lookup --file ids.txt --format json
htan download synapse download <entityId>     # or
htan download gen3 download <drs_uri> --credentials credentials.json
```

### File mapping

`htan.files` (`htan files`) bridges BigQuery results and downloads using the HTAN portal's DRS mapping file (~67,000 files):

```bash
htan files update                              # download/refresh cache
htan files lookup HTA9_1_19512                 # look up file ID
htan files lookup HTA9_1_19512 --format json   # JSON with download cmds
htan files lookup --file ids.txt               # batch lookup
htan files stats                               # mapping statistics
```

Cache: `~/.cache/htan-skill/crdcgc_drs_mapping.json` (auto-downloaded on first use).

The `infer_access_tier(file_id, level, assay)` function in `htan.files` implements the open vs controlled rules.

## CLI reference

```bash
# Portal
htan query portal tables
htan query portal describe files
htan query portal files --organ Breast --assay "scRNA-seq" --limit 10
htan query portal sql "SELECT atlas_name, COUNT(*) as n FROM files GROUP BY atlas_name"
htan query portal manifest HTA9_1_19512 --output-dir ./manifests

# BigQuery
htan query bq query "How many patients with breast cancer in HTAN?"
htan query bq sql "SELECT COUNT(*) FROM ..."
htan query bq tables
htan query bq describe clinical_tier1_demographics

# Downloads
htan download synapse download syn26535909
htan download synapse download syn26535909 --output-dir ./data --dry-run
htan download gen3 download "drs://dg.4DFC/guid-here" --credentials credentials.json
htan download gen3 resolve "drs://dg.4DFC/guid-here"

# Publications
htan pubs search
htan pubs search --keyword "spatial transcriptomics"
htan pubs search --author "Sorger PK" --format json
htan pubs fetch 12345678
htan pubs fulltext "tumor microenvironment"

# Data model
htan model fetch
htan model components
htan model attributes "scRNA-seq Level 1"
htan model describe "Library Construction Method"
htan model valid-values "File Format"
htan model search "barcode"
htan model required "Biospecimen"
htan model deps "scRNA-seq Level 1"

# File mapping
htan files update
htan files lookup HTA9_1_19512 HTA9_1_19553
htan files lookup --file ids.txt --format json
htan files stats

# Config
htan config check
```

## PubMed search for HTAN publications

HTAN publications cite specific NCI grants and authors.

**Phase 1 grants (CA233xxx)**: CA233195, CA233238, CA233243, CA233254, CA233262, CA233280, CA233284, CA233285, CA233291, CA233303, CA233311

**Phase 2 grants (CA294xxx)**: CA294459, CA294507, CA294514, CA294518, CA294527, CA294532, CA294536, CA294548, CA294551, CA294552

**DCC contract**: HHSN261201500003I

The full list of HTAN PI last authors is encoded in `htan.pubs`. Rate limits: 3 req/sec without API key, 10/sec with key.

## Security requirements

- **Never log or display credentials/tokens**
- **Validate user inputs** before passing to APIs (Synapse IDs, DRS URIs, SQL)
- **Block write operations** in BigQuery and portal SQL queries (DELETE, DROP, UPDATE, INSERT, CREATE, ALTER, TRUNCATE)
- **Sanitize SQL** — parameterize where possible, never string-interpolate user input
- **DRS URI validation**: verify format before attempting resolution
- **File path validation**: prevent path traversal in download destinations

## Testing

### Unit tests

```bash
uv run pytest tests/ -v          # all 323 tests
uv run pytest tests/ -k portal   # portal tests only
```

### CLI smoke tests (no credentials)

```bash
htan query portal tables
htan query portal describe files
htan query portal files --organ Breast --limit 5
htan pubs search --max-results 5
htan pubs search --keyword "spatial transcriptomics" --max-results 3
htan model components
htan model attributes "scRNA-seq Level 1"
htan files update
htan files lookup HTA9_1_19512
htan files stats
```

### Tests requiring credentials

```bash
htan download synapse download syn26535909 --dry-run
export GOOGLE_CLOUD_PROJECT="your-project-id"
htan query bq tables
htan download gen3 resolve "drs://dg.4DFC/your-guid"
```

## Release process

1. Update `version` in `pyproject.toml` (e.g. `0.2.0` → `0.2.1`).
2. Commit and tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. Build: `uv build` → produces `dist/htan-X.Y.Z-py3-none-any.whl` + sdist.
4. Publish: `uv publish` (requires `UV_PUBLISH_TOKEN` or `~/.pypirc`).
5. Verify on https://pypi.org/project/htan/.

## HTAN atlas centers

| Atlas | Cancer | Phase |
|---|---|---|
| HTAN HTAPP | Pan-cancer | 1 |
| HTAN HMS | Melanoma, breast, colorectal | 1 |
| HTAN OHSU | Breast | 1 |
| HTAN MSK | Colorectal, pancreatic | 1 |
| HTAN Stanford | Breast | 1 |
| HTAN Vanderbilt | Colorectal | 1 |
| HTAN WUSTL | Breast, pancreatic | 1 |
| HTAN CHOP | Pediatric | 1 |
| HTAN Duke | Breast | 1 |
| HTAN BU | Lung (pre-cancer) | 1 |
| HTAN DFCI | Multiple myeloma | 1 |
| HTAN TNP SARDANA | Multiple | 2 |
| HTAN TNP SRRS | Multiple | 2 |
| HTAN TNP TMA | Multiple | 2 |
