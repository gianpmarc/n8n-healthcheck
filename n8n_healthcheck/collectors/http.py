"""n8n HTTP endpoints: /healthz, /healthz/readiness and /metrics."""

import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from ..models import Target
from ._util import TIMEOUT, warn


def collect(target: Target) -> dict:
    if "@" in urlsplit(target.url).netloc:
        raise ValueError("credentials in the URL are not supported; pass the URL without user:password@")
    base = target.url.rstrip("/")
    fields = {
        "health": {
            "healthz": _status(base + "/healthz") == 200,
            "readiness": _status(base + "/healthz/readiness") == 200,
            "metrics_disabled": False,
        }
    }
    try:
        with urllib.request.urlopen(base + "/metrics", timeout=TIMEOUT) as response:
            metrics = parse_metrics(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 404:  # metrics disabled (N8N_METRICS=false), not an error
            fields["health"]["metrics_disabled"] = True
        else:
            warn("metrics", error)
    except OSError as error:
        warn("metrics", error)
    else:
        fields["metrics"] = metrics
        fields["n8n_version"] = version_from_metrics(metrics)
    return fields


def _status(url: str) -> int:
    """HTTP status of a GET. An unreachable host raises, so health stays 'not collected'."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return response.status
    except urllib.error.HTTPError as error:
        error.close()
        return error.code


def parse_metrics(text: str) -> dict:
    """Prometheus text exposition -> {'name' or 'name{labels}': value}."""
    metrics = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "}" in line:  # label values may contain spaces, so split after the closing brace
            end = line.rindex("}") + 1
            key, rest = line[:end], line[end:]
        else:
            key, _, rest = line.partition(" ")
        try:
            metrics[key] = float(rest.split()[0])  # an optional timestamp may follow
        except (ValueError, IndexError):
            continue
    return metrics


def version_from_metrics(metrics: dict) -> str | None:
    """n8n exposes its version as n8n_version_info{version="v1.2.3",...} 1.

    The prefix is configurable (N8N_METRICS_PREFIX), so only the suffix is matched;
    n8n_nodejs_version_info is the Node.js version and must not be picked up.
    """
    for key in metrics:
        match = re.search(r'(?<!nodejs_)version_info\{[^}]*\bversion="v?([^"]+)"', key)
        if match:
            return match.group(1)
    return None
