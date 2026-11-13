"""Helpers shared by the collectors."""

import re
import subprocess
import sys
from urllib.parse import parse_qsl, unquote, urlsplit

from ..models import REDACTED

TIMEOUT = 5  # seconds, for every network call and subprocess

# An environment variable whose name contains one of these (case-insensitive) is treated
# as a secret: its value never enters a Snapshot. PASS also covers PASSWORD and PASSWD.
SECRET_NAME_PARTS = ("PASS", "SECRET", "TOKEN", "KEY", "CREDENTIAL", "CERT")
# Exact names that match a part above but hold a setting, not a secret.
NOT_SECRET = frozenset(
    {
        "N8N_REDIS_KEY_PREFIX",
        "N8N_METRICS_INCLUDE_CREDENTIAL_TYPE_LABEL",
        "N8N_METRICS_INCLUDE_API_KEY_LABEL",
    }
)
# A value that embeds credentials whatever its name, e.g. HTTP_PROXY=http://user:pw@proxy:3128
URL_USERINFO = re.compile(r"://[^/\s]*@")


def redact(name: str, value: str) -> str:
    """REDACTED for a non-empty secret value, so rules can still tell set from unset."""
    name = name.upper()
    if value and name not in NOT_SECRET and any(part in name for part in SECRET_NAME_PARTS):
        return REDACTED
    if URL_USERINFO.search(value):
        return REDACTED
    return value


def url_secrets(*urls: str | None) -> set:
    """The passwords embedded in the given URLs, both as written and percent-decoded."""
    secrets = set()
    for url in urls:
        try:
            parts = urlsplit(url or "")
        except ValueError:
            continue
        if parts.password:
            secrets |= {parts.password, unquote(parts.password)}
        secrets |= {value for key, value in parse_qsl(parts.query) if key == "password"}
    return secrets


def warn(source: str, error: object, secrets: set = frozenset()) -> None:
    """One stderr line for a source that could not be collected. Never prints credentials."""
    message = str(error)
    for secret in sorted(secrets, key=len, reverse=True):
        message = message.replace(secret, REDACTED)
    # Safety net for a URL whose password could not be parsed out (e.g. an unencoded "/").
    message = re.sub(r"://\S*@", f"://{REDACTED}@", message)
    print(f"{source}: not collected ({message})", file=sys.stderr)


def run(args: list, env: dict | None = None, timeout: int = TIMEOUT) -> str:
    """Run a command and return its stdout; raise RuntimeError with a short message otherwise."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise RuntimeError(f"{args[0]} not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{args[0]} timed out after {timeout}s") from None
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        raise RuntimeError(lines[0] if lines else f"{args[0]} exited with {proc.returncode}")
    return proc.stdout
