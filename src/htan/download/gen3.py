"""Download HTAN controlled-access data from CRDC/Gen3 via DRS URIs.

Usage as library::

    from htan.download.gen3 import download, resolve
    path = download("drs://dg.4DFC/guid-here", output_dir="./data")
    url = resolve("drs://dg.4DFC/guid-here")

Usage as CLI::

    htan download gen3 download "drs://dg.4DFC/guid-here"
    htan download gen3 resolve "drs://dg.4DFC/guid-here"
"""

import json
import os
import re
import sys
import urllib.request

import click


GEN3_ENDPOINT = "https://nci-crdc.datacommons.io"
DRS_URI_PATTERN = re.compile(r"^drs://(dg\.4DFC|nci-crdc\.datacommons\.io/dg\.4DFC)/[a-zA-Z0-9._/\-]+$")
GUID_PATTERN = re.compile(r"^[a-zA-Z0-9._/\-]+$")


def _validate_drs_uri(uri):
    if not DRS_URI_PATTERN.match(uri):
        raise ValueError(f"Invalid DRS URI '{uri}'. Expected format: drs://dg.4DFC/<guid>")
    return uri


def _extract_guid(drs_uri):
    for prefix in ("drs://nci-crdc.datacommons.io/dg.4DFC/", "drs://dg.4DFC/"):
        if drs_uri.startswith(prefix):
            return drs_uri[len(prefix):]
    return drs_uri


def _find_credentials():
    env_path = os.environ.get("GEN3_API_KEY")
    if env_path:
        path = os.path.expanduser(env_path)
        if os.path.exists(path):
            return path
    default_path = os.path.expanduser("~/.gen3/credentials.json")
    if os.path.exists(default_path):
        return default_path
    return None


def _get_gen3_auth(credentials_file=None):
    try:
        from gen3.auth import Gen3Auth
    except ImportError:
        print("Error: gen3 package not installed. Run: pip install htan[gen3]", file=sys.stderr)
        sys.exit(1)

    if credentials_file:
        if not os.path.exists(credentials_file):
            raise ValueError(f"Credentials file not found: {credentials_file}")
        creds_path = credentials_file
    else:
        creds_path = _find_credentials()
        if not creds_path:
            print("Error: No Gen3 credentials found.", file=sys.stderr)
            print("Provide credentials, set GEN3_API_KEY, or place at ~/.gen3/credentials.json", file=sys.stderr)
            sys.exit(1)

    print(f"Using credentials: {creds_path}", file=sys.stderr)
    try:
        return Gen3Auth(endpoint=GEN3_ENDPOINT, refresh_file=creds_path)
    except Exception as e:
        print(f"Error: Gen3 authentication failed: {e}", file=sys.stderr)
        sys.exit(1)


def resolve(drs_uri, credentials=None, protocol="s3"):
    """Resolve a DRS URI to a signed download URL.

    Args:
        drs_uri: DRS URI (e.g., "drs://dg.4DFC/guid-here").
        credentials: Path to Gen3 credentials JSON. If None, auto-detected.
        protocol: Download protocol ("s3" or "gs").

    Returns:
        Signed download URL string.
    """
    _validate_drs_uri(drs_uri)
    guid = _extract_guid(drs_uri)

    auth = _get_gen3_auth(credentials)
    from gen3.file import Gen3File
    file_client = Gen3File(endpoint=GEN3_ENDPOINT, auth_provider=auth)

    try:
        url_info = file_client.get_presigned_url(guid, protocol=protocol)
        if "url" not in url_info:
            raise RuntimeError(f"Could not resolve GUID {guid}. Response: {url_info}")
        return url_info["url"]
    except Exception as e:
        print(f"Error: Failed to resolve GUID {guid}: {e}", file=sys.stderr)
        sys.exit(1)


def download(drs_uri, output_dir=".", credentials=None, protocol="s3", dry_run=False):
    """Download a file by DRS URI.

    Args:
        drs_uri: DRS URI (e.g., "drs://dg.4DFC/guid-here").
        output_dir: Directory to download to.
        credentials: Path to Gen3 credentials JSON. If None, auto-detected.
        protocol: Download protocol ("s3" or "gs").
        dry_run: If True, validate inputs without downloading.

    Returns:
        Local file path of downloaded file (or None for dry-run).
    """
    _validate_drs_uri(drs_uri)
    guid = _extract_guid(drs_uri)

    if dry_run:
        print(f"Dry run — would download:", file=sys.stderr)
        print(f"  DRS URI: {drs_uri}", file=sys.stderr)
        print(f"  GUID: {guid}", file=sys.stderr)
        print(f"  Output: {output_dir}", file=sys.stderr)
        return None

    signed_url = resolve(drs_uri, credentials=credentials, protocol=protocol)

    output_dir = os.path.realpath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    filename = guid.replace("/", "_")
    output_path = os.path.join(output_dir, filename)

    if os.path.exists(output_path):
        print(f"Skipping (already exists): {output_path}", file=sys.stderr)
        return output_path

    print(f"Downloading to {output_path}...", file=sys.stderr)
    try:
        req = urllib.request.Request(signed_url)
        with urllib.request.urlopen(req) as response:
            total_size = response.headers.get("Content-Length")
            total_size = int(total_size) if total_size else None
            downloaded = 0
            with open(output_path, "wb") as f:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size:
                        pct = downloaded * 100 / total_size
                        print(f"\r  {downloaded:,} / {total_size:,} bytes ({pct:.1f}%)", end="", file=sys.stderr)
                    else:
                        print(f"\r  {downloaded:,} bytes", end="", file=sys.stderr)
            print(file=sys.stderr)
        print(f"Downloaded: {output_path}", file=sys.stderr)
        return output_path
    except Exception as e:
        print(f"\nError: Download failed: {e}", file=sys.stderr)
        if os.path.exists(output_path):
            os.remove(output_path)
        sys.exit(1)


# --- CLI ---

_GEN3_EPILOG = """\
Examples:

  htan download gen3 download "drs://dg.4DFC/guid" --credentials creds.json
  htan download gen3 download "drs://dg.4DFC/guid" --dry-run
  htan download gen3 resolve "drs://dg.4DFC/guid"
"""


@click.group(name="gen3", epilog=_GEN3_EPILOG)
def gen3():
    """Download HTAN controlled-access data from CRDC/Gen3."""


@gen3.command(name="download")
@click.argument("drs_uri", required=False)
@click.option("--manifest", "-m", help="File with DRS URIs (one per line)")
@click.option("--credentials", "-c", help="Path to Gen3 credentials JSON")
@click.option("--output-dir", "-o", "output_dir", default=".", show_default=True)
@click.option("--protocol", type=click.Choice(["s3", "gs"]), default="s3", show_default=True)
@click.option("--dry-run", "dry_run", is_flag=True)
def download_cmd(drs_uri, manifest, credentials, output_dir, protocol, dry_run):
    """Download files by DRS URI (or via a manifest file)."""
    if manifest:
        if not os.path.exists(manifest):
            click.echo(f"Error: Manifest file not found: {manifest}", err=True)
            raise click.exceptions.Exit(1)
        uris = []
        with open(manifest) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    uris.append(line)
    elif drs_uri:
        uris = [drs_uri]
    else:
        click.echo("Error: Provide a DRS URI or --manifest file.", err=True)
        raise click.exceptions.Exit(1)

    for i, uri in enumerate(uris, 1):
        if len(uris) > 1:
            click.echo(f"\n[{i}/{len(uris)}]", err=True)
        path = download(uri, output_dir=output_dir, credentials=credentials,
                        protocol=protocol, dry_run=dry_run)
        if path:
            click.echo(path)


@gen3.command(name="resolve")
@click.argument("drs_uri")
@click.option("--credentials", "-c", help="Path to Gen3 credentials JSON")
@click.option("--protocol", type=click.Choice(["s3", "gs"]), default="s3", show_default=True)
@click.option("--dry-run", "dry_run", is_flag=True)
def resolve_cmd(drs_uri, credentials, protocol, dry_run):
    """Resolve DRS URI to a signed download URL."""
    if dry_run:
        _validate_drs_uri(drs_uri)
        guid = _extract_guid(drs_uri)
        click.echo("Dry run — would resolve:", err=True)
        click.echo(f"  DRS URI: {drs_uri}", err=True)
        click.echo(f"  GUID: {guid}", err=True)
        return

    url = resolve(drs_uri, credentials=credentials, protocol=protocol)
    click.echo(url)


def cli_main(argv=None):
    """Backward-compatible entry point — invokes the Click :data:`gen3` group."""
    try:
        return gen3.main(args=argv, prog_name="htan download gen3", standalone_mode=False)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except click.exceptions.ClickException as e:
        e.show()
        sys.exit(e.exit_code)
