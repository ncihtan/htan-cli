# Quickstart

This page walks through a complete end-to-end workflow: discover files via the
portal, look up download coordinates, then fetch a file from Synapse.

## 1. Configure credentials

```bash
htan init
htan config check
```

## 2. Find files of interest

The HTAN portal database is the most direct entry point. Filter by organ,
assay, atlas, or any other column on the `files` table.

```bash
htan query portal tables                                    # Show all tables
htan query portal describe files                            # Schema for files
htan query portal files \
  --organ Breast \
  --assay "scRNA-seq" \
  --level "Level 1" \
  --output json \
  --limit 5
```

For ad-hoc analytical queries, use `sql`:

```bash
htan query portal sql \
  "SELECT atlas_name, COUNT(*) AS n FROM files GROUP BY atlas_name ORDER BY n DESC"
```

## 3. Generate a download manifest

```bash
htan query portal manifest HTA9_1_19512 HTA9_1_19553 --output-dir ./manifests
```

This writes `synapse_manifest.tsv` and/or `gen3_manifest.json` depending on
which platform each file lives on.

## 4. Download

For open-access files (Synapse):

```bash
htan download synapse syn26535909 --output-dir ./data
```

For controlled-access files (CRDC/Gen3):

```bash
htan download gen3 download "drs://dg.4DFC/<guid>" \
  --credentials ~/.gen3/credentials.json \
  --output-dir ./data
```

## 5. Use the library directly

Everything the CLI does is also exposed as Python:

```python
from htan.query.portal import PortalClient

client = PortalClient()
files = client.find_files(organ="Breast", assay="scRNA-seq", limit=10)
for row in files:
    print(row["DataFileID"], row["Filename"])
```

## See also

- [CLI reference](cli/index.md) — the full command tree, generated from Click.
- [API reference](api/index.md) — module-by-module Python API.
