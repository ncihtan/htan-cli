"""Tests for htan.cli — Click command tree composition.

After the migration to Click, dispatch is handled by Click's group/subcommand
mechanism rather than by hand-rolled ``_dispatch_*`` helpers. We verify here
that subcommands resolve to the expected groups and produce sensible help/usage
output for invalid inputs.
"""

from click.testing import CliRunner

from htan.cli import cli


def _run(args):
    runner = CliRunner()
    return runner.invoke(cli, args)


# ===========================================================================
# Top-level structure
# ===========================================================================

def test_cli_help():
    result = _run(["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    for cmd in ("query", "download", "pubs", "model", "files", "init", "config"):
        assert cmd in result.output


def test_cli_version():
    result = _run(["--version"])
    assert result.exit_code == 0
    assert "htan" in result.output


def test_cli_unknown_command():
    result = _run(["definitely-not-a-real-command"])
    assert result.exit_code != 0


def test_cli_no_args_shows_usage():
    result = _run([])
    # Click groups exit 2 when no subcommand is supplied; usage goes to stdout.
    assert result.exit_code == 2
    assert "Usage:" in result.output


# ===========================================================================
# Subgroup composition
# ===========================================================================

def test_query_group_lists_backends():
    result = _run(["query", "--help"])
    assert result.exit_code == 0
    assert "portal" in result.output
    assert "bq" in result.output


def test_query_no_backend():
    result = _run(["query"])
    assert result.exit_code == 2


def test_query_unknown_backend():
    result = _run(["query", "unknown-backend"])
    assert result.exit_code != 0


def test_download_group_lists_backends():
    result = _run(["download", "--help"])
    assert result.exit_code == 0
    assert "synapse" in result.output
    assert "gen3" in result.output


def test_download_no_backend():
    result = _run(["download"])
    assert result.exit_code == 2


def test_download_unknown_backend():
    result = _run(["download", "ftp"])
    assert result.exit_code != 0


# ===========================================================================
# config
# ===========================================================================

def test_config_check_emits_json():
    result = _run(["config", "check"])
    assert result.exit_code == 0
    assert '"ok": true' in result.output


def test_config_help():
    result = _run(["config", "--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_config_unknown_subcommand():
    result = _run(["config", "definitely-not-a-real-subcommand"])
    assert result.exit_code != 0


# ===========================================================================
# Top-level subcommands resolve
# ===========================================================================

def test_pubs_resolves():
    result = _run(["pubs", "--help"])
    assert result.exit_code == 0
    assert "search" in result.output


def test_model_resolves():
    result = _run(["model", "--help"])
    assert result.exit_code == 0
    assert "components" in result.output


def test_files_resolves():
    result = _run(["files", "--help"])
    assert result.exit_code == 0
    assert "lookup" in result.output


def test_init_resolves():
    result = _run(["init", "--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_query_portal_resolves():
    result = _run(["query", "portal", "--help"])
    assert result.exit_code == 0
    assert "tables" in result.output
    assert "describe" in result.output
