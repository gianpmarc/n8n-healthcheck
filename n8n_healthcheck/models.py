"""Shared contract between collectors, rules, engine and report.

Collectors fill a Snapshot. Rules read a Snapshot and return a Finding or None.
A Snapshot section that is None means "not collected": rules must return None
for it, never guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

# Collectors replace the value of any secret-looking variable (password, key,
# token, secret) with REDACTED before it enters a Snapshot, so a saved snapshot
# is safe to share. Rules can still tell "set" from "unset".
REDACTED = "<redacted>"

AREAS = ("scalability", "reliability", "performance", "maintainability", "security")
SEVERITIES = ("critical", "warning", "info")


@dataclass
class Target:
    """What to inspect. Every field is optional; None means skip that source."""

    url: Optional[str] = None  # n8n base URL
    env_file: Optional[str] = None  # KEY=VALUE file with the n8n environment
    postgres_dsn: Optional[str] = None
    redis_url: Optional[str] = None
    kube_namespace: Optional[str] = None
    kube_context: Optional[str] = None


@dataclass
class Snapshot:
    """Everything collected from one instance. JSON-serialisable via asdict()."""

    # {"healthz": bool, "readiness": bool}
    health: Optional[dict] = None
    # Prometheus samples: 'name' or 'name{label="x"}' -> value
    metrics: Optional[dict] = None
    # n8n environment variables, e.g. {"EXECUTIONS_MODE": "queue"}
    env: Optional[dict] = None
    # {"version": str, "execution_count": int, "execution_table_bytes": int,
    #  "oldest_execution_age_days": float, "connections": int, "max_connections": int}
    postgres: Optional[dict] = None
    # {"version": str, "waiting_jobs": int, "active_jobs": int, "failed_jobs": int,
    #  "used_memory_bytes": int, "maxmemory_bytes": int, "maxmemory_policy": str}
    # The three job counts are None when the queue key was not found.
    redis: Optional[dict] = None
    # {"deployments": [{"name": str, "replicas": int, "ready_replicas": int,
    #   "containers": [{"name": str, "image": str, "command": [str], "env": {str: str},
    #     "requests": {"cpu": str, "memory": str}, "limits": {"cpu": str, "memory": str},
    #     "liveness_probe": bool, "readiness_probe": bool, "env_complete": bool}]}]}
    # Container env: a value taken from a Secret is "<valueFrom>" (set, value unknown).
    # env_complete is False when part of the environment could not be read (e.g. envFrom
    # a Secret), so an absent variable there does not prove the n8n default applies.
    kubernetes: Optional[dict] = None
    n8n_version: Optional[str] = None


@dataclass
class Finding:
    rule_id: str  # stable, e.g. "SCAL-001"
    area: str  # one of AREAS
    severity: str  # one of SEVERITIES
    title: str
    evidence: str  # what was observed, with the actual values
    impact: str  # what goes wrong if left as is
    recommendation: str  # the concrete fix


Rule = Callable[[Snapshot], Optional[Finding]]
