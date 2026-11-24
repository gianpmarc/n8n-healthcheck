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
    # The "Debug information" text of n8n, already read by debug_info.parse(): the
    # text itself is never kept. It is a source like the others and may be the only one.
    debug_info: Optional[dict] = None
    # An n8n API key, used next to `url` to read usage numbers. It is never copied into
    # a Snapshot, a report, a profile, a log line or an error message.
    api_key: Optional[str] = None


@dataclass
class Snapshot:
    """Everything collected from one instance. JSON-serialisable via asdict()."""

    # {"healthz": bool, "readiness": bool, "metrics_disabled": bool}
    # metrics_disabled is True only when /metrics answered 404, i.e. the endpoint is
    # switched off. Any other failure to read /metrics leaves it False (not collected).
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
    # What debug_info.parse() kept of n8n's "Debug information" text: known values, numbers
    # and versions only, never an identifier or free text.
    # {"generated_on": "YYYY-MM-DD" | None, "n8n_version": str, "node_version": str | None,
    #  "platform": "docker_self_hosted" | "docker_cloud" | "npm" | None,
    #  "database": "sqlite" | "postgresdb" | "mysqldb" | "mariadb" | None,
    #  "execution_mode": "regular" | "queue" | None, "multi_main": bool | None,
    #  "concurrency_limit": int | "unlimited" | None,
    #  "license": "community" | "enterprise_production" | "enterprise_sandbox" | None,
    #  "save_success": "all" | "none" | None, "save_error": "all" | "none" | None,
    #  "save_progress": bool | None, "save_manual": bool | None,
    #  "binary_mode": "default" | "filesystem" | "s3" | "azure" | "database" | None,
    #  "pruning": {"enabled": bool, "max_age_hours": int | None, "max_count": int | None} | None,
    #  "insecure": {"secure_cookie": bool, "file_access_blocked": bool},
    #  "cluster": None | {"instances": {"main": int, "worker": int, "webhook": int, "engine": int},
    #                     "versions": [str], "checks_failed": [str] | None}}
    # `insecure` holds True for a setting only when the text flagged it. `checks_failed`
    # names each failed check ("other" for a name the tool does not know) and is None when
    # the text listed no check. It is not one of the data sources the report counts.
    debug_info: Optional[dict] = None
    # Usage numbers read through the n8n API with a key (collectors/api.py): counts, known
    # words and a period only, never the key, an identifier or text of an answer.
    # {"executions": None | {"method": "insights" | "listed", "total": int,
    #                        "failed": int | None, "period_days": number, "estimate": bool,
    #                        "estimate_reason": "page_limit" | "short_history" | "saved_only" | None},
    #  "active_workflows": int | None, "users": int | None,
    #  "refused": {"executions": R, "users": R, "active_workflows": R},
    #  "whole_instance": bool, "key_unencrypted": bool}
    # `failed` is None when the list does not say how executions ended (n8n 1.x). R says
    # why a number is None: an HTTP status, or "limit", "slow", "large", "short",
    # "unreachable", "invalid", "redirect"; None when it was read. `whole_instance` is True
    # only when the key could read a user's detail, as only an owner or an admin may;
    # otherwise the listed executions and the active workflows may be a part of the
    # instance. Read through usage.of(). No rule reads it, and it is not one of the data
    # sources the report counts.
    usage: Optional[dict] = None


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
