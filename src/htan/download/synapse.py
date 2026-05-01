"""Download HTAN open-access files from Synapse by entity ID.

Usage as library::

    from htan.download.synapse import download
    path = download("syn26535909", output_dir="./data")

Usage as CLI::

    htan download synapse syn26535909
    htan download synapse syn26535909 --output-dir ./data --dry-run
"""

import os
import re
import sys

import click


SYNAPSE_ID_PATTERN = re.compile(r"^syn\d+$")


def _validate_synapse_id(synapse_id):
    if not SYNAPSE_ID_PATTERN.match(synapse_id):
        raise ValueError(f"Invalid Synapse ID '{synapse_id}'. Must match 'synNNNNNN'.")
    return synapse_id


def _get_synapse_client():
    try:
        import synapseclient
    except ImportError:
        print("Error: synapseclient not installed. Run: pip install htan[synapse]", file=sys.stderr)
        sys.exit(1)

    syn = synapseclient.Synapse()
    try:
        syn.login(silent=True)
    except synapseclient.core.exceptions.SynapseAuthenticationError:
        print("Error: Synapse authentication failed.", file=sys.stderr)
        print("Set SYNAPSE_AUTH_TOKEN or configure ~/.synapseConfig", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: Could not connect to Synapse: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        from synapseclient.models import UserProfile
        profile = UserProfile.from_id(syn.credentials.owner_id)
        print(f"Authenticated as: {profile.username}", file=sys.stderr)
    except Exception:
        print("Authenticated.", file=sys.stderr)
    return syn


def download(synapse_id, output_dir=".", dry_run=False):
    """Download a file from Synapse by entity ID.

    Args:
        synapse_id: Synapse entity ID (e.g., "syn26535909").
        output_dir: Directory to download to (default: current dir).
        dry_run: If True, only fetch metadata without downloading.

    Returns:
        Local file path of downloaded file (or None for dry-run).
    """
    _validate_synapse_id(synapse_id)
    output_dir = os.path.realpath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    syn = _get_synapse_client()

    from synapseclient.operations import get as syn_get
    from synapseclient.operations.factory_operations import FileOptions

    if dry_run:
        print(f"Fetching metadata for {synapse_id}...", file=sys.stderr)
        try:
            entity = syn_get(synapse_id, file_options=FileOptions(download_file=False), synapse_client=syn)
        except Exception as e:
            print(f"Error: Could not access {synapse_id}: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Dry run — {entity.name}", file=sys.stderr)
        if hasattr(entity, "content_size") and entity.content_size:
            print(f"  Size: {entity.content_size} bytes", file=sys.stderr)
        print(f"  Would download to: {output_dir}", file=sys.stderr)
        return None

    print(f"Downloading {synapse_id} to {output_dir}...", file=sys.stderr)
    try:
        entity = syn_get(synapse_id, file_options=FileOptions(download_file=True, download_location=output_dir), synapse_client=syn)
    except Exception as e:
        print(f"Error: Could not download {synapse_id}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Downloaded: {entity.path}", file=sys.stderr)
    return entity.path


# --- CLI ---

_SYNAPSE_EPILOG = """\
Examples:

  htan download synapse syn26535909
  htan download synapse syn26535909 --output-dir ./data
  htan download synapse syn26535909 --dry-run
"""


@click.command(name="synapse", epilog=_SYNAPSE_EPILOG)
@click.argument("synapse_id")
@click.option("--output-dir", "-o", "output_dir", default=".", show_default=True,
              help="Output directory")
@click.option("--dry-run", "dry_run", is_flag=True,
              help="Show metadata without downloading")
def synapse(synapse_id, output_dir, dry_run):
    """Download HTAN open-access files from Synapse by entity ID."""
    path = download(synapse_id, output_dir=output_dir, dry_run=dry_run)
    if path:
        click.echo(path)


def cli_main(argv=None):
    """Backward-compatible entry point — invokes the Click :data:`synapse` command."""
    try:
        return synapse.main(args=argv, prog_name="htan download synapse", standalone_mode=False)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except click.exceptions.ClickException as e:
        e.show()
        sys.exit(e.exit_code)
