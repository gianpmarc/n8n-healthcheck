"""Reliability rules: what loses work or takes the instance down."""

from __future__ import annotations

from .. import debug_info
from ..models import Finding, Outcome, Snapshot, Variant
from ._helpers import (
    NOT_APPLICABLE,
    NOT_STATED,
    PASSED,
    UNREADABLE,
    env_flag,
    is_queue_mode,
    n8n_containers,
    no_environment,
    num,
    queue_only,
    rule,
    shown,
    size,
    source_missing,
    undecided,
    unknown,
    with_debug_info,
)

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


def _no_redis(snapshot: Snapshot) -> Outcome:
    """Redis was not given: it holds the queue, which an instance known to run in regular mode does not have."""
    env = with_debug_info(snapshot, "EXECUTIONS_MODE")
    if env is not None and queue_only(env) is NOT_APPLICABLE:
        return NOT_APPLICABLE
    return source_missing("redis")


QUEUE_ON_SQLITE = Variant(
    title="Queue mode on SQLite",
    impact=(
        "SQLite in queue mode is unsupported by n8n: main and workers must all use one "
        "database, which a local SQLite file cannot provide (for example, workers on "
        "another host cannot open it at all), so executions fail or are lost."
    ),
    remedy=(
        "Move to PostgreSQL: set DB_TYPE=postgresdb with DB_POSTGRESDB_HOST, "
        "DB_POSTGRESDB_DATABASE, DB_POSTGRESDB_USER and DB_POSTGRESDB_PASSWORD on the "
        "main process and every worker, migrating data with the n8n export/import CLI."
    ),
)
# The database types of n8n that every process can share.
SHARED_DATABASES = ("postgresdb", "mysqldb", "mariadb")


# https://docs.n8n.io/deploy/host-n8n/install-options/production-requirements#database
@rule(
    "REL-001",
    "reliability",
    subject="Database used in queue mode",
    passed="Queue mode runs on a database that every process can share, not on SQLite.",
    variants=(QUEUE_ON_SQLITE,),
    facts=("executions.mode", "database.engine"),
    settings=("EXECUTIONS_MODE", "DB_TYPE"),
)
def sqlite_in_queue_mode(snapshot: Snapshot) -> Finding | Outcome:
    env = with_debug_info(snapshot, "EXECUTIONS_MODE", "DB_TYPE")
    if env is None:
        return no_environment(snapshot, text=True)
    if not is_queue_mode(env):
        return queue_only(env)
    # DB_TYPE_FILE reads the value from a file we cannot see.
    if "DB_TYPE_FILE" in env or unknown(env, "DB_TYPE"):
        return undecided(env, "DB_TYPE") or UNREADABLE
    database = str(env.get("DB_TYPE", "sqlite")).strip().lower()
    if database != "sqlite":
        return PASSED if database in SHARED_DATABASES else NOT_STATED
    return sqlite_in_queue_mode.info.finding(
        QUEUE_ON_SQLITE,
        "critical",
        f"{shown(env, 'EXECUTIONS_MODE')}; {shown(env, 'DB_TYPE')} (default: sqlite)."
        f"{env.note}",
    )


MAY_EVICT = Variant(
    title="Redis may evict queue keys under memory pressure",
    impact=(
        "When Redis reaches maxmemory it silently deletes keys, which can include queued "
        "jobs and job locks, so executions disappear from the queue or are marked as "
        "crashed with no error pointing at Redis."
    ),
    remedy=(
        "Set maxmemory-policy noeviction on the Redis used by n8n (CONFIG SET "
        "maxmemory-policy noeviction, or the parameter group on a managed service, whose "
        "default is often volatile-lru), so a full Redis fails writes visibly instead."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/install-options/use-a-cloud-provider/deploy-to-aws-with-terraform/scale-and-run-at-high-availability#redis-eviction-policy
@rule(
    "REL-002",
    "reliability",
    subject="Redis eviction policy",
    passed="Redis does not evict queue keys: its policy is noeviction, or it has no memory limit.",
    variants=(MAY_EVICT,),
    facts=("queue.maxmemory_policy", "queue.maxmemory_bytes"),
)
def redis_eviction_policy(snapshot: Snapshot) -> Finding | Outcome:
    redis = snapshot.redis
    if redis is None:
        return _no_redis(snapshot)
    policy = redis.get("maxmemory_policy")
    maxmemory = num(redis, "maxmemory_bytes")
    # Redis only evicts when maxmemory is set; 0 means no limit, so the policy is inert.
    if policy is None or policy == "noeviction" or not maxmemory:
        if policy is None or (policy != "noeviction" and maxmemory is None):
            return NOT_STATED
        return PASSED
    return redis_eviction_policy.info.finding(
        MAY_EVICT,
        "warning",
        f"Redis maxmemory-policy is {policy}; maxmemory is {size(maxmemory)}.",
    )


def _memory_near_limit(at_limit: str) -> Variant:
    return Variant(
        title="Redis memory close to maxmemory",
        impact=f"At maxmemory, with this policy, Redis {at_limit}.",
        remedy=(
            "Raise maxmemory (or the node size of a managed Redis), sizing it for the queue "
            "and n8n's cache together, and do not share this Redis with other applications. "
            "If large webhook responses are the cause, lower N8N_WEBHOOK_RESPONSE_RELAY_SIZE_MAX "
            "or offload them with N8N_WEBHOOK_RESPONSE_RELAY_OFFLOAD_ENABLED=true."
        ),
    )


# One per behaviour of Redis at the limit: which one applies is told by the policy.
MEMORY_REJECTS = _memory_near_limit("rejects new writes, so n8n can no longer enqueue executions or renew job locks")
MEMORY_EVICTS = _memory_near_limit("evicts keys, which can include queued jobs and job locks")


# https://docs.n8n.io/deploy/host-n8n/install-options/use-a-cloud-provider/deploy-to-aws-with-terraform/scale-and-run-at-high-availability#redis-eviction-policy
@rule(
    "REL-003",
    "reliability",
    subject="Redis memory in use against its limit",
    passed=f"Redis memory in use is below {REDIS_MEMORY_USAGE_WARN:.0%} of maxmemory.",
    variants=(MEMORY_REJECTS, MEMORY_EVICTS),
    facts=("queue.maxmemory_bytes", "queue.maxmemory_policy"),
)
def redis_memory_near_limit(snapshot: Snapshot) -> Finding | Outcome:
    redis = snapshot.redis
    if redis is None:
        return _no_redis(snapshot)
    used = num(redis, "used_memory_bytes")
    maxmemory = num(redis, "maxmemory_bytes")
    if used is None or not maxmemory:
        # maxmemory 0 is "no limit": there is nothing to come close to.
        return NOT_APPLICABLE if maxmemory == 0 else NOT_STATED
    usage = used / maxmemory
    if usage < REDIS_MEMORY_USAGE_WARN:
        return PASSED
    policy = redis.get("maxmemory_policy")
    return redis_memory_near_limit.info.finding(
        MEMORY_REJECTS if policy == "noeviction" else MEMORY_EVICTS,
        "warning",
        f"Redis uses {size(used)} of {size(maxmemory)} ({usage:.0%}; warning threshold "
        f"{REDIS_MEMORY_USAGE_WARN:.0%}); maxmemory-policy is {policy}.",
    )


QUEUE_STALLED = Variant(
    title="Queued executions are not being picked up",
    impact=(
        "No worker is processing the queue, so production executions (webhooks, "
        "schedules, triggers) are accepted but never run."
    ),
    remedy=(
        "Check that worker processes (`n8n worker`) are running and healthy, that "
        "they use the same QUEUE_BULL_REDIS_HOST, QUEUE_BULL_REDIS_PORT, "
        "QUEUE_BULL_REDIS_DB and QUEUE_BULL_PREFIX as the main process, and that "
        "EXECUTIONS_MODE=queue is set on them."
    ),
)
QUEUE_BACKLOG = Variant(
    title="Execution queue backlog",
    impact=(
        "Workers are saturated, so every new execution waits behind the backlog and "
        "webhook callers and scheduled workflows see growing delays or timeouts."
    ),
    remedy=(
        "Add worker replicas, or raise worker concurrency (--concurrency flag or "
        "N8N_CONCURRENCY_PRODUCTION_LIMIT) if workers have spare CPU and memory. "
        "To track the backlog over time, set N8N_METRICS_INCLUDE_QUEUE_METRICS=true "
        "(n8n_scaling_mode_queue_jobs_waiting); n8n does not support that setting in "
        "a multi-main setup, where the Bull queue length in Redis is the signal to "
        "watch and autoscale on."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/enable-queue-mode#how-it-works
@rule(
    "REL-004",
    "reliability",
    subject="Execution queue backlog",
    passed="The execution queue is being consumed and has no backlog.",
    variants=(QUEUE_STALLED, QUEUE_BACKLOG),
    facts=("queue.waiting_jobs", "queue.active_jobs"),
)
def queue_backlog(snapshot: Snapshot) -> Finding | Outcome:
    info = queue_backlog.info
    redis = snapshot.redis
    if redis is None:
        return _no_redis(snapshot)
    waiting = num(redis, "waiting_jobs")
    active = num(redis, "active_jobs")
    if waiting is None or active is None:
        return NOT_STATED
    evidence = f"{waiting} jobs waiting and {active} active in the Bull queue."
    if active == 0 and waiting >= STALLED_QUEUE_MIN_WAITING:
        return info.finding(QUEUE_STALLED, "critical", evidence)
    if waiting >= BACKLOG_MIN_WAITING:
        return info.finding(QUEUE_BACKLOG, "warning", evidence)
    return PASSED


NOT_READY = Variant(
    title="Readiness check is failing",
    impact=(
        "/healthz/readiness only returns 200 when the database is connected and "
        "migrated, so the instance cannot currently load or save workflows and "
        "executions."
    ),
    remedy=(
        "Check the n8n logs for database connection or migration errors and verify the "
        "DB_POSTGRESDB_* settings and that Postgres is reachable from the n8n process. "
        "If N8N_ENDPOINT_HEALTH was changed, probe that path instead of /healthz."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/keep-n8n-running/monitor-n8n
@rule(
    "REL-005",
    "reliability",
    subject="Readiness of the instance",
    passed="The readiness check answers: the database is connected and migrated.",
    variants=(NOT_READY,),
)
def readiness_failing(snapshot: Snapshot) -> Finding | Outcome:
    health = snapshot.health
    if health is None:
        return source_missing("url")
    if health.get("readiness") is not False:
        return PASSED if health.get("readiness") is True else NOT_STATED
    return readiness_failing.info.finding(
        NOT_READY,
        "critical",
        f"/healthz ok: {health.get('healthz')}; /healthz/readiness ok: "
        f"{health.get('readiness')}.",
    )


ONE_MAIN = Variant(
    title="Single main process (no multi-main setup)",
    impact=(
        "The one main process is a single point of failure: while it is down or being "
        "upgraded there is no editor or API, and schedules, polling triggers and "
        "webhooks (unless served by webhook processors) do not fire."
    ),
    remedy=(
        "If that downtime is not acceptable, run two or more main processes with "
        "N8N_MULTI_MAIN_SETUP_ENABLED=true on each, behind a load balancer with sticky "
        "sessions. Multi-main requires an Enterprise licence; without one, plan "
        "maintenance windows for upgrades."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/enable-queue-mode#multi-main-setup
@rule(
    "REL-006",
    "reliability",
    subject="Multi-main setup in queue mode",
    passed="The multi-main setup is enabled.",
    variants=(ONE_MAIN,),
    facts=("executions.mode", "multi_main.enabled"),
    settings=("EXECUTIONS_MODE", "N8N_MULTI_MAIN_SETUP_ENABLED"),
)
def single_main(snapshot: Snapshot) -> Finding | Outcome:
    multi_main = "N8N_MULTI_MAIN_SETUP_ENABLED"
    env = with_debug_info(snapshot, "EXECUTIONS_MODE", multi_main)
    if env is None:
        return no_environment(snapshot, text=True)
    if not is_queue_mode(env):
        return queue_only(env)
    if env_flag(env, multi_main) is True or unknown(env, multi_main):
        return undecided(env, multi_main) or PASSED
    return single_main.info.finding(
        ONE_MAIN,
        "info",
        f"{shown(env, 'EXECUTIONS_MODE')}; "
        f"{shown(env, 'N8N_MULTI_MAIN_SETUP_ENABLED')} (default: false).{env.note}",
    )


NO_PROBES = Variant(
    title="n8n containers without liveness or readiness probes",
    impact=(
        "Kubernetes cannot tell a hung n8n process from a healthy one: it is never "
        "restarted, and during rollouts traffic reaches pods before their database "
        "connection and migrations are ready."
    ),
    remedy=(
        "Add an HTTP livenessProbe on /healthz and a readinessProbe on "
        "/healthz/readiness (port 5678). Workers only serve these endpoints when "
        "QUEUE_HEALTH_CHECK_ACTIVE=true is set (port: QUEUE_HEALTH_CHECK_PORT)."
    ),
)


# https://github.com/n8n-io/n8n-hosting/blob/main/charts/n8n/README.md#ports-and-health-checks
@rule(
    "REL-007",
    "reliability",
    subject="Liveness and readiness probes of the n8n containers",
    passed="Every n8n container has a liveness and a readiness probe.",
    variants=(NO_PROBES,),
)
def missing_probes(snapshot: Snapshot) -> Finding | Outcome:
    if snapshot.kubernetes is None:
        return source_missing("kubernetes")
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
        # Without an n8n container there is nothing that was verified.
        return PASSED if any(n8n_containers(snapshot.kubernetes)) else NOT_STATED
    return missing_probes.info.finding(NO_PROBES, "warning", "; ".join(missing) + ".")


NO_RESOURCES = Variant(
    title="n8n containers without resource requests or a memory limit",
    impact=(
        "Without requests the scheduler can pack n8n onto an overcommitted node, where "
        "it is among the first pods evicted under memory pressure; without a memory "
        "limit one runaway execution can starve every other pod on that node."
    ),
    remedy=(
        "Set resources.requests.cpu, resources.requests.memory and "
        "resources.limits.memory on every n8n container (the official Helm chart "
        "defaults main and worker pods to requests of 200m CPU / 512Mi and a 1Gi "
        "memory limit), then size them from observed usage."
    ),
)


# https://github.com/n8n-io/n8n-hosting/blob/main/charts/n8n/values.yaml
# https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
@rule(
    "REL-008",
    "reliability",
    subject="Resource requests and memory limit of the n8n containers",
    passed="Every n8n container has CPU and memory requests and a memory limit.",
    variants=(NO_RESOURCES,),
)
def missing_resources(snapshot: Snapshot) -> Finding | Outcome:
    if snapshot.kubernetes is None:
        return source_missing("kubernetes")
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
        return PASSED if any(n8n_containers(snapshot.kubernetes)) else NOT_STATED
    return missing_resources.info.finding(NO_RESOURCES, "warning", "; ".join(missing) + ".")


CHECK_FAILED = Variant(
    title="A cluster check failed",
    impact=(
        "n8n runs these checks on the processes that registered with the instance. A "
        "failed one means they do not form one consistent cluster: for example two "
        "processes sharing a host id (hostid-clash), more than one leader "
        "(split-brain) or processes on different versions (version-mismatch). "
        "Scheduled work can then run twice or not at all."
    ),
    remedy=(
        "Open Help > About n8n > Debug information on the instance again to see whether "
        "the check still fails, then read the logs of every main, worker and webhook "
        "process around that time. Give every process its own host id and the same n8n "
        "version, and make sure only one main is the leader."
    ),
)


# n8n's instance registry runs these checks itself; the debug information reports each
# one's status. Only the text states them, so there is no probe to give way to.
@rule(
    "REL-009",
    "reliability",
    subject="Cluster checks that n8n runs itself",
    passed="None of the cluster checks that n8n runs itself failed.",
    variants=(CHECK_FAILED,),
    facts=("cluster.checks_failed",),
)
def cluster_check_failed(snapshot: Snapshot) -> Finding | Outcome:
    if debug_info.of(snapshot) is None:
        return source_missing("debug_info")
    failed = ((debug_info.of(snapshot) or {}).get("cluster") or {}).get("checks_failed")
    if not failed:
        # None: no cluster section, or it listed no check. An empty list: every check passed.
        return PASSED if failed is not None else NOT_STATED
    known = [name for name in failed if name != debug_info.OTHER_CHECK]
    others = len(failed) - len(known)
    if others:
        # A check this tool has no name for is counted, its name never written.
        known.append(f"{others} other check{'s' if others != 1 else ''}")
    return cluster_check_failed.info.finding(
        CHECK_FAILED,
        "warning",
        f"The {debug_info.phrase(debug_info.of(snapshot))} lists "
        f"{len(failed)} failed cluster check{'s' if len(failed) != 1 else ''}: {', '.join(known)}.",
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
    cluster_check_failed,
]
