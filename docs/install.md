# Installation

The `htan` package is published on PyPI as [`htan`](https://pypi.org/project/htan/)
and requires Python 3.10 or newer.

## Quick install

```bash
pip install htan
```

This installs the CLI and library along with all default dependencies
(Synapse client, Gen3 SDK, Google BigQuery client, pandas).

## With `uv` (recommended for development)

```bash
uv pip install htan                  # in an active venv
uv pip install -e ".[dev,docs]"      # editable, with test + docs deps
```

## First-run setup

After installing, run the interactive wizard:

```bash
htan init
```

This walks through credential setup for each backend (Synapse, portal
ClickHouse, BigQuery, CRDC/Gen3). You can rerun it at any point with
`htan init --force` or check the current state with `htan init --status`.

Credentials live in the conventional locations:

| Service | Location |
|---------|----------|
| Portal ClickHouse | OS keychain or `~/.config/htan-skill/portal.json` |
| Synapse | `SYNAPSE_AUTH_TOKEN` env var or `~/.synapseConfig` |
| BigQuery | `gcloud auth application-default login` (or service account JSON) |
| CRDC/Gen3 | `~/.gen3/credentials.json` (download from CRDC after dbGaP auth) |

## Verifying the install

```bash
htan --version
htan config check
htan query portal tables          # requires portal credentials
```
