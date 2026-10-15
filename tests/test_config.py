"""Tests for htan.config — credential management."""

import json
import os
import pytest

from htan.config import (
    _validate_config,
    _load_from_env,
    _load_from_file,
    load_portal_config,
    detect_source,
    get_clickhouse_url,
    get_default_database,
    check_setup,
    ConfigError,
    REQUIRED_KEYS,
)


VALID_CREDS = {
    "host": "example.com",
    "port": "8443",
    "user": "testuser",
    "password": "testpass",
}


# --- _validate_config ---

def test_validate_config_all_keys_present():
    assert _validate_config(VALID_CREDS) == []


def test_validate_config_missing_keys():
    missing = _validate_config({"host": "x"})
    assert "port" in missing
    assert "user" in missing
    assert "password" in missing


def test_validate_config_empty_dict():
    missing = _validate_config({})
    assert set(missing) == set(REQUIRED_KEYS)


# --- _load_from_env ---

def test_load_from_env_valid(monkeypatch):
    monkeypatch.setenv("HTAN_PORTAL_CREDENTIALS", json.dumps(VALID_CREDS))
    cfg = _load_from_env()
    assert cfg is not None
    assert cfg["host"] == "example.com"


def test_load_from_env_not_set(monkeypatch):
    monkeypatch.delenv("HTAN_PORTAL_CREDENTIALS", raising=False)
    assert _load_from_env() is None


def test_load_from_env_invalid_json(monkeypatch):
    monkeypatch.setenv("HTAN_PORTAL_CREDENTIALS", "not-json")
    assert _load_from_env() is None


def test_load_from_env_missing_keys(monkeypatch):
    monkeypatch.setenv("HTAN_PORTAL_CREDENTIALS", json.dumps({"host": "x"}))
    assert _load_from_env() is None


# --- _load_from_file ---

def test_load_from_file_valid(tmp_path):
    f = tmp_path / "portal.json"
    f.write_text(json.dumps(VALID_CREDS))
    cfg = _load_from_file(str(f))
    assert cfg is not None
    assert cfg["user"] == "testuser"


def test_load_from_file_missing():
    assert _load_from_file("/nonexistent/path.json") is None


def test_load_from_file_invalid_json(tmp_path):
    f = tmp_path / "portal.json"
    f.write_text("not json")
    assert _load_from_file(str(f)) is None


def test_load_from_file_incomplete(tmp_path):
    f = tmp_path / "portal.json"
    f.write_text(json.dumps({"host": "x"}))
    assert _load_from_file(str(f)) is None


# --- load_portal_config ---

def test_load_portal_config_from_env(monkeypatch):
    monkeypatch.setenv("HTAN_PORTAL_CREDENTIALS", json.dumps(VALID_CREDS))
    cfg = load_portal_config()
    assert cfg["host"] == "example.com"


def test_load_portal_config_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv("HTAN_PORTAL_CREDENTIALS", raising=False)
    # Mock keychain to return None so we fall through to file
    monkeypatch.setattr("htan.config._load_from_keychain", lambda: None)
    f = tmp_path / "portal.json"
    f.write_text(json.dumps(VALID_CREDS))
    cfg = load_portal_config(config_path=str(f))
    assert cfg["host"] == "example.com"


def test_load_portal_config_fetches_public_default(monkeypatch, tmp_path):
    """With no env/keychain/file, resolution fetches the public read-only creds."""
    monkeypatch.delenv("HTAN_PORTAL_CREDENTIALS", raising=False)
    monkeypatch.setattr("htan.config._load_from_keychain", lambda: None)
    fetched = {"host": "h", "port": 8443, "user": "htanwebuser",
               "password": "p", "default_database": "auto"}
    monkeypatch.setattr("htan.config._load_public_default", lambda: dict(fetched))
    cfg = load_portal_config(config_path=str(tmp_path / "missing.json"))
    assert cfg["user"] == "htanwebuser"


def test_load_portal_config_raises_when_offline(monkeypatch, tmp_path):
    """No config anywhere and portal unreachable -> ConfigError."""
    monkeypatch.delenv("HTAN_PORTAL_CREDENTIALS", raising=False)
    monkeypatch.setattr("htan.config._load_from_keychain", lambda: None)
    monkeypatch.setattr("htan.config._load_public_default", lambda: None)
    with pytest.raises(ConfigError, match="could not"):
        load_portal_config(config_path=str(tmp_path / "missing.json"))


def test_parse_creds_from_bundle():
    """Extract creds from a minified portal bundle snippet."""
    from htan.config import _parse_creds_from_bundle
    js = (
        'x(){let _="HT4N_secret";if(!_)throw Error("NEXT_PUBLIC_CLICKHOUSE_PASSWORD '
        'is not configured");return}'
        'createClient({url:"https://abc.us-east-1.aws.clickhouse.cloud:8443/db",'
        'username:"htanwebuser",password:_})'
    )
    cfg = _parse_creds_from_bundle(js)
    assert cfg["user"] == "htanwebuser"
    assert cfg["password"] == "HT4N_secret"
    assert cfg["host"] == "abc.us-east-1.aws.clickhouse.cloud"
    assert cfg["port"] == 8443


def test_parse_creds_from_bundle_no_match():
    """Chunks without the password marker return None."""
    from htan.config import _parse_creds_from_bundle
    assert _parse_creds_from_bundle("some unrelated js") is None


# --- detect_source ---

def test_detect_source_env(monkeypatch):
    monkeypatch.setenv("HTAN_PORTAL_CREDENTIALS", json.dumps(VALID_CREDS))
    assert detect_source() == "env"


def test_detect_source_never_none(monkeypatch):
    monkeypatch.delenv("HTAN_PORTAL_CREDENTIALS", raising=False)
    # May find keychain or file on the developer machine; otherwise the baked-in
    # default always applies, so this never returns None.
    result = detect_source()
    assert result in ("env", "keychain", "file", "default")


def test_detect_source_default(monkeypatch):
    monkeypatch.delenv("HTAN_PORTAL_CREDENTIALS", raising=False)
    monkeypatch.setattr("htan.config._load_from_keychain", lambda: None)
    monkeypatch.setattr("htan.config._load_from_file", lambda config_path=None: None)
    assert detect_source() == "default"


# --- get_clickhouse_url ---

def test_get_clickhouse_url():
    url = get_clickhouse_url({"host": "ch.example.com", "port": "8443"})
    assert url == "https://ch.example.com:8443/"


# --- get_default_database ---

def test_get_default_database_auto():
    assert get_default_database({"default_database": "auto"}) is None


def test_get_default_database_empty():
    assert get_default_database({}) is None


def test_get_default_database_explicit():
    assert get_default_database({"default_database": "htan_2024"}) == "htan_2024"


# --- check_setup ---

def test_check_setup_returns_all_keys():
    status = check_setup()
    assert "synapse" in status
    assert "portal" in status
    assert "gen3" in status
    assert "bigquery" in status
    assert "python" in status
    assert status["python"]["sufficient"] is True
