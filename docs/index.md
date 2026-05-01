# htan

Python tools for accessing Human Tumor Atlas Network (HTAN) data.

The `htan` package provides:

- **A unified `htan` CLI** for the HTAN portal database, BigQuery metadata,
  Synapse and CRDC/Gen3 downloads, the data model, and PubMed publication search.
- **A Python library** wrapping the same functionality, suitable for use in
  notebooks and pipelines.

```{tip}
New to the project? Start with [Installation](install.md), then
[Quickstart](quickstart.md), then look up specific commands in the [CLI
reference](cli/index.md).
```

## At a glance

```bash
pip install htan
htan init                           # First-run wizard
htan query portal files --organ Breast --limit 10
htan query bq sql "SELECT COUNT(*) FROM ..."
htan download synapse syn26535909
htan download gen3 download "drs://dg.4DFC/<guid>"
htan pubs search --keyword "spatial transcriptomics"
htan model components
htan files lookup HTA9_1_19512
```

## Data access tiers

HTAN data has multiple access levels. The portal provides a unified query
interface; downloads route through Synapse (open access) or CRDC/Gen3
(controlled access).

| Tier | Source | Auth | Module |
|------|--------|------|--------|
| Portal metadata + file discovery | ClickHouse | Synapse team membership | {mod}`htan.query.portal` |
| Open access (de-identified, processed) | Synapse | PAT | {mod}`htan.download.synapse` |
| Controlled access (raw, protected) | CRDC/Gen3 | dbGaP + Gen3 creds | {mod}`htan.download.gen3` |
| Metadata query | BigQuery (`isb-cgc-bq`) | ADC | {mod}`htan.query.bq` |

```{toctree}
:maxdepth: 2
:caption: User guide

install
quickstart
```

```{toctree}
:maxdepth: 2
:caption: Reference

cli/index
api/index
```
