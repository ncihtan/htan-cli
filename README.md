# htan

Python CLI and library for accessing [Human Tumor Atlas Network (HTAN)](https://humantumoratlas.org) data — an NCI Cancer Moonshot initiative constructing 3D atlases of how human cancers evolve from precancerous lesions to advanced disease.

```bash
pip install htan
```

This package provides a single `htan` command that unifies access to four HTAN data platforms (portal ClickHouse, Synapse, Gen3/CRDC, ISB-CGC BigQuery), HTAN publication search via PubMed, and HTAN data model queries.

> Looking for the Claude Code plugin? See [`ncihtan/htan-claude`](https://github.com/ncihtan/htan-claude). It uses this package as its CLI backend.

## Capabilities

| Command | Auth required | Description |
|---|---|---|
| `htan query portal …` | Synapse team membership | Query file metadata, clinical data, download coordinates (ClickHouse) |
| `htan query bq …` | Google Cloud ADC | Query HTAN metadata tables in ISB-CGC BigQuery |
| `htan download synapse …` | Synapse token | Download open-access data (processed matrices, clinical) |
| `htan download gen3 …` | Gen3 credentials + dbGaP | Download controlled-access data (raw sequencing) |
| `htan pubs …` | None | Search HTAN-affiliated publications by keyword, author, year |
| `htan model …` | None | Query HTAN data model components, attributes, controlled vocabularies |
| `htan files …` | None | Resolve HTAN file IDs to Synapse/Gen3 download coordinates |
| `htan config check` | None | Show which credentials are configured |

All commands accept `--help` for full usage.

## Quick start

```bash
pip install htan
htan init                      # interactive credential setup
htan query portal tables       # list available portal tables
htan query portal files --organ Breast --assay "scRNA-seq" --limit 20
htan pubs search --keyword "spatial transcriptomics"
htan model components
htan files lookup HTA9_1_19512
```

## Authentication

Credentials are stored in standard config locations, never in environment variables echoed to your shell.

| Service | How to set up |
|---|---|
| **Portal** | Join [HTAN Claude Skill Users](https://www.synapse.org/Team:3574960), then run `htan init` |
| **Synapse** | Get a Personal Access Token from synapse.org, configure `~/.synapseConfig` |
| **Gen3/CRDC** | Request dbGaP access for study `phs002371`, download credentials from the CRDC portal |
| **BigQuery** | Run `gcloud auth application-default login` and set `GOOGLE_CLOUD_PROJECT` |

`htan config check` prints which services are currently configured.

## Data access tiers

HTAN files fall into three access tiers; this package picks the right platform automatically.

| Tier | Platform | Identifier in portal |
|---|---|---|
| Open (Level 3+, Auxiliary) | Synapse | `entityId` (e.g. `syn26535909`) |
| Controlled (raw sequencing) | Gen3/CRDC | `drs_uri` (e.g. `drs://dg.4DFC/<guid>`) |
| Imaging (mixed) | Synapse or CRDC-GC | depends on dbGaP set |

The portal query result includes both `synapseId` and `drs_uri` columns when present, so a single query is enough to plan downloads across tiers.

## Python API

```python
from htan.query.portal import PortalClient
from htan.pubs import search_publications

client = PortalClient()
df = client.query("SELECT atlas_name, COUNT(*) FROM files GROUP BY atlas_name")

pubs = search_publications(keyword="spatial transcriptomics")
```

See `src/htan/` for the full module layout. The CLI in `htan.cli` is the canonical entry point and demonstrates expected usage of each client.

## Development

```bash
git clone https://github.com/ncihtan/htan-cli.git
cd htan-cli
uv venv && uv pip install -e ".[dev]"
uv run pytest tests/                  # 323 tests
uv run htan --help
```

Dependencies are managed with [`uv`](https://github.com/astral-sh/uv). Tests are pure Python (`pytest`); no integration credentials required.

## License

MIT — see [LICENSE.txt](LICENSE.txt).
