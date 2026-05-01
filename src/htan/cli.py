"""Unified CLI for HTAN tools.

Entry point: ``htan`` command (installed via ``pip install htan``).

Subcommands::

    htan query portal ...    Portal ClickHouse queries
    htan query bq ...        BigQuery queries
    htan download synapse ... Synapse open-access downloads
    htan download gen3 ...   Gen3/CRDC controlled-access downloads
    htan pubs ...            PubMed publication search
    htan model ...           HTAN data model queries
    htan files ...           File ID to download coordinate mapping
    htan init ...            First-run setup wizard
    htan config check        Credential status

Built on Click — the top-level :data:`cli` group composes subgroups defined in
each submodule. Submodules also expose ``cli_main(argv)`` shims for callers
that want to invoke a subtree directly with a list of args.
"""

import json

import click

from htan import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="htan")
def cli():
    """HTAN — Python tools for accessing Human Tumor Atlas Network data."""


# --- query --------------------------------------------------------------

@cli.group()
def query():
    """Query HTAN data via the portal or BigQuery."""


# Subcommands attached lazily so heavy imports stay lazy.
def _add_query_subcommands():
    from htan.query.portal import portal
    from htan.query.bq import bq
    query.add_command(portal, name="portal")
    query.add_command(bq, name="bq")


_add_query_subcommands()


# --- download -----------------------------------------------------------

@cli.group()
def download():
    """Download HTAN files from Synapse or Gen3/CRDC."""


def _add_download_subcommands():
    from htan.download.synapse import synapse
    from htan.download.gen3 import gen3
    download.add_command(synapse, name="synapse")
    download.add_command(gen3, name="gen3")


_add_download_subcommands()


# --- pubs / model / files / init ---------------------------------------

def _add_top_level_subcommands():
    from htan.pubs import pubs
    from htan.model import model
    from htan.files import files
    from htan.init import init
    cli.add_command(pubs, name="pubs")
    cli.add_command(model, name="model")
    cli.add_command(files, name="files")
    cli.add_command(init, name="init")


_add_top_level_subcommands()


# --- config -------------------------------------------------------------

@cli.group()
def config():
    """Credential configuration."""


@config.command(name="check")
def config_check():
    """Print the current credential configuration as JSON."""
    from htan.config import check_setup
    status = check_setup()
    click.echo(json.dumps({"ok": True, "status": status}, indent=2))


@config.command(name="init-portal", hidden=True)
def config_init_portal():
    """Deprecated alias for ``htan init portal``."""
    click.echo("Deprecated: use 'htan init portal' instead.", err=True)
    from htan.init import cli_main as init_main
    init_main(["portal"])


def main():
    """Entry point for the ``htan`` script (defined in pyproject.toml)."""
    cli(prog_name="htan")
