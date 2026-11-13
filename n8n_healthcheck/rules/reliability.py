"""Reliability rules: what loses work or takes the instance down."""

from __future__ import annotations

from typing import Optional

from ..models import Finding, Snapshot
from ._helpers import env_flag, is_queue_mode, n8n_containers, num, shown

# Redis starts evicting (or, with noeviction, rejecting writes) at maxmemory. 80% leaves
# headroom for a burst: each large webhook response in flight can hold about
# 1.5 x N8N_WEBHOOK_RESPONSE_RELAY_SIZE_MAX (default 64 MiB). Heuristic, not an n8n number.
REDIS_MEMORY_USAGE_WARN = 0.80

# Jobs sit in the Bull "wait" list only until a worker has a free slot. With zero active
# jobs, even a few waiting ones mean nothing is consuming the queue; 10 avoids flagging
# the instant between enqueue and pickup on a healthy instance.
STALLED_QUEUE_MIN_WAITING = 10

# With workers busy, a backlog of 100 is ten default-concurrency workers' worth of
# pending executions: load is arriving faster than it is processed. Heuristic.
BACKLOG_MIN_WAITING = 100


# https://docs.n8n.io/deploy/host-n8n/install-options/production-requirements#database
def sqlite_in_queue_mode(snapshot: Snapshot) -> Optional[Finding]:
    env = snapshot.env
    if env is None or not is_queue_mode(env):
        return None
    # DB_TYPE_FILE reads the value from a file we cannot see.
    if "DB_TYPE_FILE" in env:
        return None
    if str(env.get("DB_TYPE", "sqlite")).strip().lower() != "sqlite":
        return None
    return Finding(
        rule_id="REL-001",
        area="reliability",
        severity="critical",
        title="Queue mode on SQLite",
        evidence=f"{shown(env, 'EXECUTIONS_MODE')}; {shown(env, 'DB_TYPE')} (default: sqlite).",
        impact=(
            "SQLite in queue mode is unsupported by n8n: main and workers must all use one "
            "database, which a local SQLite file cannot provide (for example, workers on "
            "another host cannot open it at all), so executions fail or are lost."
        ),
        recommendation=(
            "Move to PostgreSQL: set DB_TYPE=postgresdb with DB_POSTGRESDB_HOST, "
            "DB_POSTGRESDB_DATABASE, DB_POSTGRESDB_USER and DB_POSTGRESDB_PASSWORD on the "
            "main process and every worker, migrating data with the n8n export/import CLI."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/install-options/use-a-cloud-provider/deploy-to-aws-with-terraform/scale-and-run-at-high-availability#redis-eviction-policy
def redis_eviction_policy(snapshot: Snapshot) -> Optional[Finding]:
    redis = snapshot.redis
    if redis is None:
        return None
    policy = redis.get("maxmemory_policy")
    maxmemory = num(redis, "maxmemory_bytes")
    # Redis only evicts when maxmemory is set; 0 means no limit, so the policy is inert.
    if policy is None or policy == "noeviction" or not maxmemory:
        return None
    return Finding(
        rule_id="REL-002",
        area="reliability",
        severity="warning",
        title="Redis may evict queue keys under memory pressure",
        evidence=f"Redis maxmemory-policy is {policy}; maxmemory is {maxmemory} bytes.",
        impact=(
            "When Redis reaches maxmemory it silently deletes keys, which can include queued "
            "jobs and job locks, so executions disappear from the queue or are marked as "
            "crashed with no error pointing at Redis."
        ),
        recommendation=(
            "Set maxmemory-policy noeviction on the Redis used by n8n (CONFIG SET "
            "maxmemory-policy noeviction, or the parameter group on a managed service, whose "
            "default is often volatile-lru), so a full Redis fails writes visibly instead."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/install-options/use-a-cloud-provider/deploy-to-aws-with-terraform/scale-and-run-at-high-availability#redis-eviction-policy
def redis_memory_near_limit(snapshot: Snapshot) -> Optional[Finding]:
    redis = snapshot.redis
    if redis is None:
        return None
    used = num(redis, "used_memory_bytes")
    maxmemory = num(redis, "maxmemory_bytes")
    if used is None or not maxmemory:
        return None
    usage = used / maxmemory
    if usage < REDIS_MEMORY_USAGE_WARN:
        return None
    policy = redis.get("maxmemory_policy")
    at_limit = (
        "rejects new writes, so n8n can no longer enqueue executions or renew job locks"
        if policy == "noeviction"
        else "evicts keys, which can include queued jobs and job locks"
    )
    return Finding(
        rule_id="REL-003",
        area="reliability",
        severity="warning",
        title="Redis memory close to maxmemory",
        evidence=(
            f"Redis uses {used} of {maxmemory} bytes ({usage:.0%}; warning threshold "
            f"{REDIS_MEMORY_USAGE_WARN:.0%}); maxmemory-policy is {policy}."
        ),
        impact=f"At maxmemory, with this policy, Redis {at_limit}.",
        recommendation=(
            "Raise maxmemory (or the node size of a managed Redis), sizing it for the queue "
            "and n8n's cache together, and do not share this Redis with other applications. "
            "If large webhook responses are the cause, lower N8N_WEBHOOK_RESPONSE_RELAY_SIZE_MAX "
            "or offload them with N8N_WEBHOOK_RESPONSE_RELAY_OFFLOAD_ENABLED=true."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/enable-queue-mode#how-it-works
def queue_backlog(snapshot: Snapshot) -> Optional[Finding]:
    redis = snapshot.redis
    if redis is None:
        return None
    waiting = num(redis, "waiting_jobs")
    active = num(redis, "active_jobs")
    if waiting is None or active is None:
        return None
    evidence = f"{waiting} jobs waiting and {active} active in the Bull queue."
    if active == 0 and waiting >= STALLED_QUEUE_MIN_WAITING:
        return Finding(
            rule_id="REL-004",
            area="reliability",
            severity="critical",
            title="Queued executions are not being picked up",
            evidence=evidence,
            impact=(
                "No worker is processing the queue, so production executions (webhooks, "
                "schedules, triggers) are accepted but never run."
            ),
            recommendation=(
                "Check that worker processes (`n8n worker`) are running and healthy, that "
                "they use the same QUEUE_BULL_REDIS_HOST, QUEUE_BULL_REDIS_PORT, "
                "QUEUE_BULL_REDIS_DB and QUEUE_BULL_PREFIX as the main process, and that "
                "EXECUTIONS_MODE=queue is set on them."
            ),
        )
    if waiting >= BACKLOG_MIN_WAITING:
        return Finding(
            rule_id="REL-004",
            area="reliability",
            severity="warning",
            title="Execution queue backlog",
            evidence=evidence,
            impact=(
                "Workers are saturated, so every new execution waits behind the backlog and "
                "webhook callers and scheduled workflows see growing delays or timeouts."
            ),
            recommendation=(
                "Add worker replicas, or raise worker concurrency (--concurrency flag or "
                "N8N_CONCURRENCY_PRODUCTION_LIMIT) if workers have spare CPU and memory. "
                "To track the backlog over time, set N8N_METRICS_INCLUDE_QUEUE_METRICS=true "
                "(n8n_scaling_mode_queue_jobs_waiting); n8n does not support that setting in "
                "a multi-main setup, where the Bull queue length in Redis is the signal to "
                "watch and autoscale on."
            ),
        )
    return None


# https://docs.n8n.io/deploy/host-n8n/keep-n8n-running/monitor-n8n
def readiness_failing(snapshot: Snapshot) -> Optional[Finding]:
    health = snapshot.health
    if health is None or health.get("readiness") is not False:
        return None
    return Finding(
        rule_id="REL-005",
        area="reliability",
        severity="critical",
        title="Readiness check is failing",
        evidence=(
            f"/healthz ok: {health.get('healthz')}; /healthz/readiness ok: "
            f"{health.get('readiness')}."
        ),
        impact=(
            "/healthz/readiness only returns 200 when the database is connected and "
            "migrated, so the instance cannot currently load or save workflows and "
            "executions."
        ),
        recommendation=(
            "Check the n8n logs for database connection or migration errors and verify the "
            "DB_POSTGRESDB_* settings and that Postgres is reachable from the n8n process. "
            "If N8N_ENDPOINT_HEALTH was changed, probe that path instead of /healthz."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/enable-queue-mode#multi-main-setup
def single_main(snapshot: Snapshot) -> Optional[Finding]:
    env = snapshot.env
    if env is None or not is_queue_mode(env):
        return None
    if env_flag(env, "N8N_MULTI_MAIN_SETUP_ENABLED") is True:
        return None
    return Finding(
        rule_id="REL-006",
        area="reliability",
        severity="info",
        title="Single main process (no multi-main setup)",
        evidence=(
            f"{shown(env, 'EXECUTIONS_MODE')}; "
            f"{shown(env, 'N8N_MULTI_MAIN_SETUP_ENABLED')} (default: false)."
        ),
        impact=(
            "The one main process is a single point of failure: while it is down or being "
            "upgraded there is no editor or API, and schedules, polling triggers and "
            "webhooks (unless served by webhook processors) do not fire."
        ),
        recommendation=(
            "If that downtime is not acceptable, run two or more main processes with "
            "N8N_MULTI_MAIN_SETUP_ENABLED=true on each, behind a load balancer with sticky "
            "sessions. Multi-main requires an Enterprise licence; without one, plan "
            "maintenance windows for upgrades."
        ),
    )


# https://github.com/n8n-io/n8n-hosting/blob/main/charts/n8n/README.md#ports-and-health-checks
def missing_probes(snapshot: Snapshot) -> Optional[Finding]:
    if snapshot.kubernetes is None:
        return None
    missing = []
    for deployment, container, _role in n8n_containers(snapshot.kubernetes):
        absent = [
            name
            for name, key in (("liveness", "liveness_probe"), ("readiness", "readiness_probe"))
            if not container.get(key)
        ]
        if absent:
            missing.append(
                f"{deployment['name']}/{container['name']} (no {' or '.join(absent)} probe)"
            )
    if not missing:
        return None
    return Finding(
        rule_id="REL-007",
        area="reliability",
        severity="warning",
        title="n8n containers without liveness or readiness probes",
        evidence="; ".join(missing) + ".",
        impact=(
            "Kubernetes cannot tell a hung n8n process from a healthy one: it is never "
            "restarted, and during rollouts traffic reaches pods before their database "
            "connection and migrations are ready."
        ),
        recommendation=(
            "Add an HTTP livenessProbe on /healthz and a readinessProbe on "
            "/healthz/readiness (port 5678). Workers only serve these endpoints when "
            "QUEUE_HEALTH_CHECK_ACTIVE=true is set (port: QUEUE_HEALTH_CHECK_PORT)."
        ),
    )


# https://github.com/n8n-io/n8n-hosting/blob/main/charts/n8n/values.yaml
# https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
def missing_resources(snapshot: Snapshot) -> Optional[Finding]:
    if snapshot.kubernetes is None:
        return None
    # A CPU limit is deliberately not required: it throttles rather than protects, and
    # leaving it out is a common, defensible choice. Memory is the one that gets pods killed.
    required = (("requests", "cpu"), ("requests", "memory"), ("limits", "memory"))
    missing = []
    for deployment, container, _role in n8n_containers(snapshot.kubernetes):
        absent = [
            f"{kind}.{resource}"
            for kind, resource in required
            if not (container.get(kind) or {}).get(resource)
        ]
        if absent:
            missing.append(f"{deployment['name']}/{container['name']} (no {', '.join(absent)})")
    if not missing:
        return None
    return Finding(
        rule_id="REL-008",
        area="reliability",
        severity="warning",
        title="n8n containers without resource requests or a memory limit",
        evidence="; ".join(missing) + ".",
        impact=(
            "Without requests the scheduler can pack n8n onto an overcommitted node, where "
            "it is among the first pods evicted under memory pressure; without a memory "
            "limit one runaway execution can starve every other pod on that node."
        ),
        recommendation=(
            "Set resources.requests.cpu, resources.requests.memory and "
            "resources.limits.memory on every n8n container (the official Helm chart "
            "defaults main and worker pods to requests of 200m CPU / 512Mi and a 1Gi "
            "memory limit), then size them from observed usage."
        ),
    )


RULES = [
    sqlite_in_queue_mode,
    redis_eviction_policy,
    redis_memory_near_limit,
    queue_backlog,
    readiness_failing,
    single_main,
    missing_probes,
    missing_resources,
]
