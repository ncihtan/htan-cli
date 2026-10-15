"""Credential management for HTAN services.

4-tier credential resolution for portal ClickHouse (all stdlib):
  1. Env var: HTAN_PORTAL_CREDENTIALS (JSON string) — for Cowork
  2. OS Keychain: macOS `security` / Linux `secret-tool` — for local
  3. Config file: ~/.config/htan-skill/portal.json — backward compat
  4. Baked-in public read-only default — so the CLI works with zero setup

Also provides setup status checks for all HTAN services.
"""

import json
import os
import platform
import shutil
import subprocess
import sys

CONFIG_DIR = os.path.expanduser("~/.config/htan-skill")
CONFIG_PATH = os.path.join(CONFIG_DIR, "portal.json")

REQUIRED_KEYS = ("host", "port", "user", "password")

# Public read-only credential (tier 4) — fetched live, never committed.
#
# The HTAN data portal website (data.humantumoratlas.org) queries ClickHouse
# directly from the browser, so it ships a read-only credential in its public
# JS bundle via Next.js NEXT_PUBLIC_* build inlining. These creds are therefore
# already public — anyone who loads the portal has them. Rather than hardcode
# the password here (which would put a scannable secret in git history), we
# fetch it from that same public bundle at runtime and cache it locally.
#
# This lets `htan query portal` work out of the box with no Synapse account,
# team membership, or `htan init`, while keeping the repo free of plaintext
# credentials. Explicit env/keychain/file config still takes precedence.
PORTAL_SITE_URL = "https://data.humantumoratlas.org/"

CACHE_DIR = os.path.expanduser("~/.cache/htan-skill")
CREDS_CACHE_PATH = os.path.join(CACHE_DIR, "portal_creds.json")
# Refresh the fetched creds if the cache is older than this (portal may rotate).
CREDS_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

KEYCHAIN_SERVICE = "htan-portal"
KEYCHAIN_ACCOUNT = "htan"

SYNAPSE_CONFIG_PATH = os.path.expanduser("~/.synapseConfig")
GEN3_CREDS_PATH = os.path.expanduser("~/.gen3/credentials.json")
BIGQUERY_ADC_PATH = os.path.expanduser(
    "~/.config/gcloud/application_default_credentials.json"
)


class ConfigError(Exception):
    """Portal configuration error — credentials missing or invalid."""
    pass


def _validate_config(cfg):
    """Validate that a config dict has all required keys. Returns list of missing keys."""
    return [k for k in REQUIRED_KEYS if k not in cfg]


def _load_from_env():
    """Load credentials from HTAN_PORTAL_CREDENTIALS env var (JSON string).

    Returns:
        Dict with credentials, or None if env var not set or invalid.
    """
    raw = os.environ.get("HTAN_PORTAL_CREDENTIALS")
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
        if _validate_config(cfg):
            return None
        return cfg
    except (json.JSONDecodeError, TypeError):
        return None


def _load_from_keychain():
    """Load credentials from OS keychain (macOS `security` / Linux `secret-tool`).

    Returns:
        Dict with credentials, or None if not found or unsupported platform.
    """
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return None
            raw = result.stdout.strip()
        elif system == "Linux":
            result = subprocess.run(
                ["secret-tool", "lookup",
                 "service", KEYCHAIN_SERVICE, "account", KEYCHAIN_ACCOUNT],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return None
            raw = result.stdout.strip()
        else:
            return None

        if not raw:
            return None
        cfg = json.loads(raw)
        if _validate_config(cfg):
            return None
        return cfg
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError,
            OSError):
        return None


def _load_from_file(config_path=None):
    """Load credentials from config file.

    Returns:
        Dict with credentials, or None if file missing or invalid.
    """
    path = config_path or CONFIG_PATH
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            cfg = json.load(f)
        if _validate_config(cfg):
            return None
        return cfg
    except (json.JSONDecodeError, PermissionError, OSError):
        return None


def save_to_keychain(creds):
    """Store credentials in OS keychain.

    Args:
        creds: Dict with portal credentials.

    Returns:
        True if stored successfully, False otherwise.
    """
    system = platform.system()
    creds_json = json.dumps(creds)
    try:
        if system == "Darwin":
            subprocess.run(
                ["security", "add-generic-password",
                 "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
                 "-w", creds_json, "-U"],
                check=True, capture_output=True, text=True, timeout=10,
            )
            return True
        elif system == "Linux":
            subprocess.run(
                ["secret-tool", "store",
                 "--label=HTAN Portal",
                 "service", KEYCHAIN_SERVICE, "account", KEYCHAIN_ACCOUNT],
                input=creds_json.encode(), check=True,
                capture_output=True, timeout=10,
            )
            return True
        else:
            return False
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError):
        return False


def _make_ssl_context():
    """Create an SSL context, preferring certifi's CA bundle if available."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _http_get(url, timeout=15):
    """Fetch a URL and return the decoded body, or None on any error."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "htan-cli"})
        with urllib.request.urlopen(req, timeout=timeout, context=_make_ssl_context()) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_creds_from_bundle(js):
    """Extract portal ClickHouse creds from a portal JS bundle chunk.

    The portal inlines its read-only credential via Next.js NEXT_PUBLIC_* build
    substitution. Returns a config dict, or None if this chunk lacks them.
    """
    import re
    if "CLICKHOUSE_PASSWORD" not in js:
        return None
    pw_m = re.search(
        r'["\']([^"\']+)["\'];if\(![^)]*\)throw [^(]*\(\s*["\']NEXT_PUBLIC_CLICKHOUSE_PASSWORD',
        js,
    )
    host_m = re.search(r'https://[a-z0-9]+\.[a-z0-9.-]+clickhouse\.cloud:\d+', js)
    user_m = re.search(r'username:\s*["\']([^"\']+)["\']', js)
    if not (pw_m and host_m):
        return None

    hostport = host_m.group(0).replace("https://", "")
    host, _, port = hostport.partition(":")
    cfg = {
        "host": host,
        "port": int(port) if port else 8443,
        "user": user_m.group(1) if user_m else "htanwebuser",
        "password": pw_m.group(1),
        "default_database": "auto",
    }
    if _validate_config(cfg):
        return None
    return cfg


def _fetch_creds_from_portal():
    """Fetch the public read-only creds from the live portal JS bundle.

    Returns a config dict, or None if the portal is unreachable or its bundle
    layout changed. Stdlib-only (urllib) to match the portal query client.
    """
    import re
    index = _http_get(PORTAL_SITE_URL)
    if not index:
        return None

    chunks = sorted(set(re.findall(r'/_next/static/[^"\' ]+\.js', index)))
    base = PORTAL_SITE_URL.rstrip("/")
    for chunk in chunks:
        js = _http_get(base + chunk)
        if not js:
            continue
        cfg = _parse_creds_from_bundle(js)
        if cfg is not None:
            return cfg
    return None


def _write_creds_cache(cfg):
    """Persist fetched creds to the local cache (best effort, mode 0600)."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CREDS_CACHE_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(CREDS_CACHE_PATH, 0o600)
    except OSError:
        pass


def _read_creds_cache(ignore_ttl=False):
    """Read cached fetched creds. Returns dict, or None if missing/stale/invalid."""
    if not os.path.exists(CREDS_CACHE_PATH):
        return None
    try:
        if not ignore_ttl:
            import time
            age = time.time() - os.path.getmtime(CREDS_CACHE_PATH)
            if age > CREDS_CACHE_TTL_SECONDS:
                return None
        with open(CREDS_CACHE_PATH, "r") as f:
            cfg = json.load(f)
        if _validate_config(cfg):
            return None
        return cfg
    except (json.JSONDecodeError, OSError):
        return None


def _load_public_default():
    """Resolve the public read-only creds: fresh cache, else fetch, else stale cache.

    Returns a config dict, or None if creds cannot be obtained (offline with no
    prior cache).
    """
    cfg = _read_creds_cache()
    if cfg is not None:
        return cfg

    cfg = _fetch_creds_from_portal()
    if cfg is not None:
        _write_creds_cache(cfg)
        return cfg

    # Fetch failed — fall back to a stale cache if we have one.
    return _read_creds_cache(ignore_ttl=True)


def detect_source():
    """Detect which credential source would be used.

    Returns:
        "env" if HTAN_PORTAL_CREDENTIALS is set and valid,
        "keychain" if OS keychain has valid credentials,
        "file" if config file exists and is valid,
        "default" if none of the above (baked-in public read-only cred).

    Never returns None — the baked-in default is always available.
    """
    if _load_from_env() is not None:
        return "env"
    if _load_from_keychain() is not None:
        return "keychain"
    if _load_from_file() is not None:
        return "file"
    return "default"


def load_portal_config(config_path=None):
    """Load portal credentials using 3-tier resolution.

    Resolution order:
      1. HTAN_PORTAL_CREDENTIALS env var (JSON string)
      2. OS Keychain (macOS security / Linux secret-tool)
      3. Config file (~/.config/htan-skill/portal.json)

    Args:
        config_path: Override path for config file (tier 3 only).

    Returns:
        Dict with keys: host, port, user, password, and optionally default_database.

    Raises:
        ConfigError if no valid credentials found in any tier.
    """
    # Tier 1: Environment variable
    cfg = _load_from_env()
    if cfg is not None:
        return cfg

    # Tier 2: OS Keychain
    cfg = _load_from_keychain()
    if cfg is not None:
        return cfg

    # Tier 3: Config file
    cfg = _load_from_file(config_path)
    if cfg is not None:
        return cfg

    # Tier 4: Public read-only creds, fetched from the portal bundle and cached.
    cfg = _load_public_default()
    if cfg is not None:
        return cfg

    raise ConfigError(
        "Portal credentials not configured and the public HTAN portal could not "
        "be reached to fetch read-only credentials.\n\n"
        "This usually means you are offline. Options:\n"
        "  1. Reconnect — the CLI auto-fetches public read-only creds from\n"
        f"     {PORTAL_SITE_URL} and caches them.\n"
        "  2. Set HTAN_PORTAL_CREDENTIALS env var (JSON string).\n"
        "  3. Create ~/.config/htan-skill/portal.json manually."
    )


def get_clickhouse_url(cfg):
    """Build the ClickHouse HTTP URL from config.

    Args:
        cfg: Config dict from load_portal_config().

    Returns:
        URL string like 'https://host:port/'
    """
    return f"https://{cfg['host']}:{cfg['port']}/"


def get_default_database(cfg):
    """Get the default database name from config, or None if auto-discover.

    Args:
        cfg: Config dict from load_portal_config().

    Returns:
        Database name string, or None if set to 'auto' (triggers discovery).
    """
    db = cfg.get("default_database", "auto")
    if db == "auto" or not db:
        return None
    return db


def check_setup():
    """Check the status of all HTAN credential configurations.

    Returns:
        Dict with status for each service (synapse, portal, gen3, bigquery, uv, python).
    """
    status = {}

    # Synapse
    has_synapse_env = bool(os.environ.get("SYNAPSE_AUTH_TOKEN"))
    has_synapse_config = os.path.exists(SYNAPSE_CONFIG_PATH)
    status["synapse"] = {
        "configured": has_synapse_env or has_synapse_config,
        "method": (
            "SYNAPSE_AUTH_TOKEN"
            if has_synapse_env
            else ("~/.synapseConfig" if has_synapse_config else None)
        ),
    }

    # Portal — check all 3 tiers (env, keychain, file)
    portal_source = detect_source()
    status["portal"] = {
        "configured": portal_source is not None,
        "source": portal_source,
        "path": CONFIG_PATH if portal_source == "file" else None,
    }

    # Gen3
    has_gen3_env = bool(
        os.environ.get("GEN3_API_KEY")
        and os.path.exists(os.environ.get("GEN3_API_KEY", ""))
    )
    has_gen3_config = os.path.exists(GEN3_CREDS_PATH)
    status["gen3"] = {
        "configured": has_gen3_env or has_gen3_config,
        "method": (
            "GEN3_API_KEY"
            if has_gen3_env
            else ("~/.gen3/credentials.json" if has_gen3_config else None)
        ),
    }

    # BigQuery
    has_bq_sa = bool(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        and os.path.exists(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))
    )
    has_bq_adc = os.path.exists(BIGQUERY_ADC_PATH)
    status["bigquery"] = {
        "configured": has_bq_sa or has_bq_adc,
        "method": (
            "GOOGLE_APPLICATION_CREDENTIALS"
            if has_bq_sa
            else ("application_default_credentials" if has_bq_adc else None)
        ),
    }

    # uv
    uv_path = shutil.which("uv")
    status["uv"] = {"available": uv_path is not None, "path": uv_path}

    # Python
    v = sys.version_info
    status["python"] = {
        "version": f"{v.major}.{v.minor}.{v.micro}",
        "sufficient": v >= (3, 10),
    }

    return status
